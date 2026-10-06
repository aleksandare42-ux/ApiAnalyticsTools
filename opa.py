#!/usr/bin/env python3
"""opa - run, control and test the Payment Ops Analytics stack.

Uses Docker when it's available. Without Docker everything runs locally:
a portable PostgreSQL (downloaded once) plus the services as Python
processes. This script only needs the standard library.

Stack:
  python opa.py up [--local | --docker] [--bi]
  python opa.py status
  python opa.py down [-v]
  python opa.py logs [service] [-f]
  python opa.py restart [service]

Data and traffic:
  python opa.py seed [--rows 500000] [--days 30] [--no-anomalies]
  python opa.py traffic start [TPS] | stop | tps N | burst N | stats
  python opa.py chaos presets | list | inject PRESET | clear
  python opa.py detect [--history]
  python opa.py anomalies [--status all]
  python opa.py pay [--merchant m_001] [--country US] [-n 10]
  python opa.py sql "SELECT ..."
  python opa.py demo [--preset provider_decline_spike]

Other:
  python opa.py test
  python opa.py psql
  python opa.py open [page]
"""

import argparse
import io
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# logs, pid file and the current mode
LOCAL = ROOT / ".local"

# PostgreSQL binaries and data go to a local disk. The project itself may
# live on a USB stick, and Postgres is very slow there.
if os.environ.get("OPA_DATA_DIR"):
    DATA_HOME = Path(os.environ["OPA_DATA_DIR"])
elif sys.platform == "win32":
    DATA_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    DATA_HOME = DATA_HOME / "opanalytics"
else:
    DATA_HOME = Path.home() / ".local" / "share" / "opanalytics"

PG_VERSION = "16.15.0"
PG_PORT = int(os.environ.get("PGPORT", 5432))

# 127.0.0.1 and not localhost: on Windows localhost tries IPv6 first and
# every new connection gets ~200ms slower.
API = os.environ.get("OPA_API", "http://127.0.0.1:8080")

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

# start order matters a bit: providers first, the gateway needs them
SERVICES = {
    "providers": 8001,
    "gateway": 8000,
    "traffic": 8002,
    "detector": 8003,
    "dashboard": 8080,
}

URLS_HELP = """
  Dashboard          http://localhost:8080
  Gateway API        http://localhost:8000/docs
  PSP simulator      http://localhost:8001/docs
  Traffic simulator  http://localhost:8002/docs
  Anomaly detector   http://localhost:8003/docs
  PostgreSQL         postgresql://opa:opa@localhost:5432/opanalytics
                     (read-only user for BI tools: analyst / analyst)
"""

EFFECT_TO_METRIC = {
    "decline_rate": "approval_rate",
    "latency_ms": "latency",
    "error_rate": "error_rate",
    "refund_rate": "refund_rate",
    "volume_mult": "volume_share",
}

COLORS = {"green": 32, "red": 31, "yellow": 33, "blue": 34, "gray": 2}


def color(text, name):
    if not sys.stdout.isatty():
        return str(text)
    return f"\033[{COLORS[name]}m{text}\033[0m"


def run(cmd, check=True, **kwargs):
    print(color("$ " + " ".join(str(part) for part in cmd), "gray"))
    return subprocess.run(cmd, check=check, **kwargs)


def api(method, path, body=None, timeout=60):
    """Call the dashboard API and return the decoded JSON."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        API + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        message = e.read().decode()[:500]
        sys.exit(color(f"API error {e.code}: {message}", "red"))
    except urllib.error.URLError as e:
        sys.exit(
            color(
                f"Can't reach the dashboard at {API} ({e.reason}). "
                "Is it running? Try: python opa.py up",
                "red",
            )
        )
    return json.loads(raw) if raw else None


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for(url, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(1)
    return False


def docker_available():
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=20
        )
    except Exception:
        return False
    return result.returncode == 0


def current_mode():
    path = LOCAL / "mode"
    if not path.exists():
        return None
    return path.read_text().strip() or None


def save_mode(mode):
    LOCAL.mkdir(exist_ok=True)
    (LOCAL / "mode").write_text(mode or "")


def print_table(rows, columns):
    widths = []
    for col in columns:
        values = [len(str(row.get(col, ""))) for row in rows]
        widths.append(max([len(col)] + values))
    print("  ".join(col.ljust(w) for col, w in zip(columns, widths)))
    for row in rows:
        cells = [str(row.get(col, "")) for col in columns]
        print("  ".join(cell.ljust(w) for cell, w in zip(cells, widths)))


def compose(*args, check=True):
    return run(["docker", "compose", *args], check=check, cwd=ROOT)


# Local mode


def ensure_venv():
    if not VENV_PYTHON.exists():
        print(color("Creating .venv ...", "blue"))
        run([sys.executable, "-m", "venv", str(ROOT / ".venv")])

    # reinstall only when requirements.txt changed
    requirements = ROOT / "requirements.txt"
    stamp = ROOT / ".venv" / ".requirements"
    text = requirements.read_text()
    if stamp.exists() and stamp.read_text() == text:
        return
    print(color("Installing dependencies ...", "blue"))
    run(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "-q",
            "-r",
            str(requirements),
        ]
    )
    stamp.write_text(text)


def pg_bin(name):
    if IS_WINDOWS:
        name += ".exe"
    return DATA_HOME / "pg" / "bin" / name


def download_postgres():
    """Download portable PostgreSQL binaries (the zonky embedded build)."""
    if sys.platform == "win32":
        build = "windows-amd64"
    elif sys.platform == "darwin":
        if platform.machine() == "arm64":
            build = "darwin-arm64v8"
        else:
            build = "darwin-amd64"
    else:
        build = "linux-amd64"

    name = f"embedded-postgres-binaries-{build}"
    url = (
        "https://repo1.maven.org/maven2/io/zonky/test/postgres/"
        f"{name}/{PG_VERSION}/{name}-{PG_VERSION}.jar"
    )
    print(color(f"Downloading PostgreSQL {PG_VERSION} ({build}) ...", "blue"))
    print(color(f"  {url}", "gray"))
    with urllib.request.urlopen(url, timeout=300) as resp:
        jar = resp.read()

    # the jar is a zip with a .txz archive inside
    with zipfile.ZipFile(io.BytesIO(jar)) as archive:
        txz_name = [n for n in archive.namelist() if n.endswith(".txz")][0]
        txz = archive.read(txz_name)

    target = DATA_HOME / "pg"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(txz), mode="r:xz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target, filter="data")
        else:
            tar.extractall(target)
    if not IS_WINDOWS:
        for path in (target / "bin").iterdir():
            path.chmod(0o755)
    print(color(f"  extracted to {target}", "green"))


def start_postgres():
    if not pg_bin("pg_ctl").exists():
        download_postgres()

    data_dir = DATA_HOME / "pgdata"
    log_dir = LOCAL / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if not (data_dir / "PG_VERSION").exists():
        print(color("Creating the database cluster ...", "blue"))
        run(
            [
                str(pg_bin("initdb")),
                "-D",
                str(data_dir),
                "-U",
                "opa",
                "-A",
                "trust",
                "-E",
                "UTF8",
                "--no-locale",
            ],
            stdout=subprocess.DEVNULL,
        )

    status = subprocess.run(
        [str(pg_bin("pg_ctl")), "status", "-D", str(data_dir)],
        capture_output=True,
    )
    if status.returncode == 0:
        print(color("PostgreSQL is already running", "green"))
    elif port_in_use(PG_PORT):
        sys.exit(
            color(
                f"Port {PG_PORT} is used by another program "
                "(another PostgreSQL?). Stop it or set PGPORT.",
                "red",
            )
        )
    else:
        options = (
            f"-p {PG_PORT} -c listen_addresses=localhost "
            "-c max_connections=200"
        )
        # Postgres must not inherit our stdout/stderr, otherwise the
        # caller's pipe stays open after we exit.
        run(
            [
                str(pg_bin("pg_ctl")),
                "start",
                "-D",
                str(data_dir),
                "-l",
                str(log_dir / "postgres.log"),
                "-w",
                "-o",
                options,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    roles_sql = ROOT / "db" / "init" / "00_roles.sql"
    subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "opanalytics.createdb",
            str(PG_PORT),
            str(roles_sql),
        ],
        env=service_env(),
        check=True,
    )


def service_env():
    db_url = f"postgresql://opa:opa@127.0.0.1:{PG_PORT}/opanalytics"
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "app"),
            "PYTHONUNBUFFERED": "1",
            "SQL_DIR": str(ROOT / "sql"),
            "DATABASE_URL": db_url,
            "GATEWAY_URL": "http://127.0.0.1:8000",
            "PROVIDERS_URL": "http://127.0.0.1:8001",
            "TRAFFIC_URL": "http://127.0.0.1:8002",
            "DETECTOR_URL": "http://127.0.0.1:8003",
            "ADMINER_URL": "http://127.0.0.1:8081",
        }
    )
    return env


def load_pids():
    path = LOCAL / "pids.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_pids(pids):
    (LOCAL / "pids.json").write_text(json.dumps(pids, indent=1))


def is_running(pid):
    if IS_WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid):
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def start_service(name):
    log = open(LOCAL / "logs" / f"{name}.log", "ab")
    kwargs = {}
    if IS_WINDOWS:
        # detached and without a console window
        create_no_window = 0x08000000
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | create_no_window
        )
    else:
        kwargs["start_new_session"] = True

    cmd = [
        str(VENV_PYTHON),
        "-m",
        "opanalytics.run",
        name,
        "--port",
        str(SERVICES[name]),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=service_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    return proc.pid


def local_up():
    ensure_venv()
    start_postgres()
    pids = load_pids()
    for name, port in SERVICES.items():
        if name in pids and is_running(pids[name]):
            print(f"  {name:10} already running (pid {pids[name]})")
            continue
        if port_in_use(port):
            sys.exit(
                color(
                    f"Port {port} for {name} is busy. "
                    "Try: python opa.py down",
                    "red",
                )
            )
        pids[name] = start_service(name)
        save_pids(pids)
        print(
            f"  {name:10} started, pid {pids[name]:<6} port {port}, "
            f"log: .local/logs/{name}.log"
        )
        if name == "providers":
            # let the first service create the schema before the rest
            time.sleep(2)


def local_down():
    for name, pid in load_pids().items():
        kill(pid)
        print(f"  {name:10} stopped")
    save_pids({})

    data_dir = DATA_HOME / "pgdata"
    if pg_bin("pg_ctl").exists() and (data_dir / "PG_VERSION").exists():
        subprocess.run(
            [str(pg_bin("pg_ctl")), "stop", "-D", str(data_dir), "-m", "fast"],
            capture_output=True,
        )
        print("  postgres   stopped")


# Commands


def cmd_up(args):
    if args.local:
        mode = "local"
    elif args.docker or docker_available():
        mode = "docker"
    else:
        mode = "local"

    if mode == "docker":
        if not docker_available():
            sys.exit(
                color(
                    "Docker is not running. Start Docker Desktop "
                    "or use: python opa.py up --local",
                    "red",
                )
            )
        profile = ["--profile", "bi"] if args.bi else []
        compose(*profile, "up", "-d", "--build")
    else:
        if args.bi:
            print(color("Metabase works only in Docker mode.", "yellow"))
        if not args.local:
            print(
                color("Docker is not available, using local mode.", "yellow")
            )
        local_up()

    save_mode(mode)
    print(color("Waiting for the dashboard ...", "blue"))
    if not wait_for(API + "/health", timeout=180):
        print(
            color(
                "The dashboard didn't start. "
                "Check: python opa.py logs dashboard",
                "red",
            )
        )
        return

    print(color(f"\nEverything is up ({mode} mode).", "green"))
    print(URLS_HELP)
    print(
        "Next: python opa.py seed, python opa.py traffic start, "
        "python opa.py demo"
    )
    if not args.no_browser:
        webbrowser.open(API)


def cmd_down(args):
    mode = current_mode()
    use_docker = mode == "docker"
    if mode is None and not args.local and docker_available():
        use_docker = True

    if use_docker:
        volumes = ["-v"] if args.volumes else []
        compose("--profile", "bi", "down", *volumes)
    else:
        local_down()
        if args.volumes:
            shutil.rmtree(DATA_HOME / "pgdata", ignore_errors=True)
            print("  database files removed")
    save_mode(None)


def cmd_restart(args):
    mode = current_mode()
    if mode == "docker":
        compose("restart", *([args.service] if args.service else []))
        return
    if mode != "local":
        sys.exit("The stack is not running.")

    names = [args.service] if args.service else list(SERVICES)
    pids = load_pids()
    for name in names:
        if name in pids:
            kill(pids[name])
    time.sleep(1.5)
    for name in names:
        pids[name] = start_service(name)
        print(f"  {name:10} restarted, pid {pids[name]}")
    save_pids(pids)


def follow_file(path):
    with open(path, errors="replace") as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            pass


def cmd_logs(args):
    if current_mode() == "docker":
        options = ["--tail", "200"]
        if args.follow:
            options.append("-f")
        if args.service:
            options.append(args.service)
        compose("logs", *options, check=False)
        return

    log_dir = LOCAL / "logs"
    if args.service:
        files = [log_dir / f"{args.service}.log"]
    else:
        files = sorted(log_dir.glob("*.log"))
    for path in files:
        if not path.exists():
            continue
        print(color(f"===== {path.name}", "blue"))
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        print("".join(lines[-60:]))

    if args.follow and args.service:
        follow_file(files[0])


def cmd_status(args):
    print(f"mode: {current_mode() or 'not started'}")
    for service in api("GET", "/api/services"):
        state = (
            color("up  ", "green") if service["ok"] else color("down", "red")
        )
        info = service.get("info")
        extra = ""
        if service["name"] == "postgres" and isinstance(info, dict):
            extra = f"{info['tx']:,} tx, {info['size']}"
        elif service["name"] == "traffic" and isinstance(info, dict):
            extra = f"running={info.get('running')} tps={info.get('tps')}"
        ms = str(service.get("ms", ""))
        print(
            f"  {state} {service['name']:10} :{service['port']:<5} "
            f"{ms:>4} ms  {extra}"
        )


def cmd_seed(args):
    job = api(
        "POST",
        "/api/seed",
        {
            "rows": args.rows,
            "days": args.days,
            "inject_anomalies": not args.no_anomalies,
            "replace": not args.append,
            "scan_after": not args.no_scan,
        },
    )
    print(f"job {job['id']} started")

    last_line = None
    while True:
        job = api("GET", f"/api/jobs/{job['id']}")
        line = f"  [{job['progress'] * 100:5.1f}%] {job['message']}"
        if line != last_line:
            print(line)
            last_line = line
        if job["status"] != "running":
            break
        time.sleep(1)

    if job["status"] != "done":
        print(color(job["status"], "red"))
        return
    print(color("done", "green"))
    if args.no_scan:
        return

    result = api("GET", "/api/evaluation")
    precision = result["precision"] or 0
    print(
        f"\nDetector vs injected anomalies: recall {result['recall']:.0%},"
        f" precision {precision:.0%}"
    )
    for item in result["truth"]:
        if item["detected"]:
            mark = color("FOUND ", "green")
        else:
            mark = color("MISSED", "red")
        print(
            f"  {mark} {item['name']:32} "
            f"{item['dimension']}={item['dim_value']} {item['metric']}"
        )


def cmd_traffic(args):
    if args.action == "start":
        body = {"running": True}
        if args.value:
            body["tps"] = float(args.value)
        print(api("PATCH", "/api/settings/traffic", body))
    elif args.action == "stop":
        print(api("PATCH", "/api/settings/traffic", {"running": False}))
    elif args.action == "tps":
        print(
            api("PATCH", "/api/settings/traffic", {"tps": float(args.value)})
        )
    elif args.action == "burst":
        count = int(args.value or 500)
        print(api("POST", f"/api/traffic/burst?count={count}"))
    else:
        print(json.dumps(api("GET", "/api/traffic/stats"), indent=1))


def cmd_chaos(args):
    if args.action == "presets":
        for key, preset in api("GET", "/api/chaos/presets").items():
            print(f"  {key:24} {preset['description']}")

    elif args.action == "list":
        rows = api("GET", "/api/chaos")
        for row in rows:
            row["live"] = "LIVE" if row["live"] else ""
        print_table(
            rows,
            [
                "id",
                "live",
                "name",
                "target_type",
                "target_value",
                "effect",
                "value",
                "expires_at",
            ],
        )

    elif args.action == "inject":
        if not args.preset:
            sys.exit(
                "usage: opa.py chaos inject PRESET "
                "(see: opa.py chaos presets)"
            )
        rule = api(
            "POST",
            f"/api/chaos/presets/{args.preset}" f"?minutes={args.minutes}",
        )
        print(
            color(
                f"injected #{rule['id']}: {rule['name']}, "
                f"expires {rule['expires_at']}",
                "yellow",
            )
        )

    elif args.action == "clear":
        api("DELETE", "/api/chaos")
        print("all chaos rules stopped")


def cmd_detect(args):
    if args.history:
        result = api("POST", "/api/detector/scan", timeout=600)
    else:
        result = api("POST", "/api/detector/run")
    print(json.dumps(result, indent=1, default=str))


def cmd_anomalies(args):
    rows = api("GET", f"/api/anomalies?status={args.status}&limit=50")
    if not rows:
        print("no anomalies")
    for row in rows:
        print(
            f"  [{row['severity']:8}] {row['status']:8} "
            f"{row['detected_by']:5} {row['message']}"
        )


def describe_attempt(attempt):
    text = f"{attempt['provider']}:{attempt['status']}"
    if attempt["decline_reason"]:
        text += f"({attempt['decline_reason']})"
    return f"{text} {attempt['processing_time_ms']}ms"


def cmd_pay(args):
    body = {
        "merchant_id": args.merchant,
        "amount": args.amount,
        "country": args.country,
        "payment_method": args.method,
    }
    if args.provider:
        body["force_provider"] = args.provider

    for _ in range(args.n):
        result = api("POST", "/api/test-payment", body)
        attempts = " -> ".join(describe_attempt(a) for a in result["attempts"])
        ok = result["status"] == "approved"
        status = color(result["status"].upper(), "green" if ok else "red")
        print(
            f"  {status:10} {result['amount']} {result['currency']}  "
            f"{attempts}"
        )


def cmd_sql(args):
    result = api(
        "POST", "/api/sql", {"query": args.query, "limit": args.limit}
    )
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    print_table(rows, result["columns"])
    print(color(f"({len(rows)} rows, {result['ms']} ms)", "gray"))


def cmd_test(args):
    if current_mode() == "docker":
        compose("exec", "dashboard", "pytest", "-q", "/srv/tests", check=False)
        return
    ensure_venv()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "app"))
    run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            "-q",
            str(ROOT / "tests"),
            *args.pytest_args,
        ],
        check=False,
        env=env,
    )


def cmd_psql(args):
    if current_mode() == "docker":
        compose(
            "exec", "postgres", "psql", "-U", "opa", "opanalytics", check=False
        )
        return
    # no psql in the portable build, use our small shell instead
    subprocess.run(
        [str(VENV_PYTHON), "-m", "opanalytics.sqlshell"], env=service_env()
    )


def step(title):
    print(color(f"\n== {title}", "blue"))


def last_minute_stats(dimension):
    column = "merchant_id" if dimension == "merchant" else dimension
    query = f"""
        SELECT {column},
               count(*) AS n,
               round(100.0 * avg((status = 'approved')::int), 1) AS approval,
               round(avg(processing_time_ms)) AS avg_ms
        FROM transactions
        WHERE source = 'live' AND created_at > now() - interval '1 minute'
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 6
    """
    rows = api("POST", "/api/sql", {"query": query})["rows"]
    parts = [
        f"{key}={approval}%/{ms}ms (n={n})" for key, n, approval, ms in rows
    ]
    return "  ".join(parts)


def wait_for_detection(target_value, metric, timeout):
    started = time.time()
    while time.time() - started < timeout:
        time.sleep(5)
        open_items = api("GET", "/api/anomalies?status=open&detected_by=live")
        elapsed = time.time() - started
        print(f"   t+{elapsed:4.0f}s  open anomalies: {len(open_items)}")
        for item in open_items:
            if item["dim_value"] == target_value and item["metric"] == metric:
                return item, elapsed
    return None, timeout


def cmd_demo(args):
    """Scripted incident: baseline, inject, detect, mitigate, recover."""
    presets = api("GET", "/api/chaos/presets")
    if args.preset not in presets:
        sys.exit(f"unknown preset, choose from: {', '.join(presets)}")
    preset = presets[args.preset]
    dimension = preset["target_type"]
    metric = EFFECT_TO_METRIC[preset["effect"]]
    old_settings = api("GET", "/api/settings")

    step(
        f"1. Healthy traffic at {args.tps} TPS, cascading on, "
        "smart routing off"
    )
    api("DELETE", "/api/anomalies?detected_by=live")
    api("DELETE", "/api/chaos")
    api(
        "PATCH",
        "/api/settings/gateway",
        {"cascade": True, "smart_routing": False},
    )
    # shorter windows than the defaults so the demo doesn't take forever
    api(
        "PATCH",
        "/api/settings/detector",
        {
            "interval_s": 10,
            "window_min": 2,
            "guard_min": 1,
            "baseline_min": 15,
        },
    )
    api("PATCH", "/api/settings/traffic", {"running": True, "tps": args.tps})
    print(f"   collecting {args.baseline}s of baseline ...")
    time.sleep(args.baseline)
    print(f"   last minute by {dimension}:", last_minute_stats(dimension))

    step(f"2. Incident: {preset['name']}. {preset['description']}")
    api("POST", f"/api/chaos/presets/{args.preset}?minutes=10")
    found, elapsed = wait_for_detection(
        preset["target_value"], metric, args.timeout
    )
    if found:
        print(
            color(
                f"   DETECTED in {elapsed:.0f}s: {found['message']}", "yellow"
            )
        )
        others = api("GET", "/api/anomalies?status=open&detected_by=live")
        others = [a for a in others if a["anomaly_id"] != found["anomaly_id"]]
        for item in others[:6]:
            print(color(f"     also: {item['message']}", "gray"))
    else:
        print(
            color(
                f"   {dimension}={preset['target_value']} {metric} "
                f"was not detected in {args.timeout}s",
                "red",
            )
        )

    if dimension == "provider":
        step(
            f"3. Mitigation: smart routing moves traffic away from "
            f"{preset['target_value']}"
        )
        api("PATCH", "/api/settings/gateway", {"smart_routing": True})
        time.sleep(40)
        routing = api("GET", "/api/providers")["routing"]
        if routing:
            weights = routing["weights_by_method"]["card"]
            total = sum(weights.values()) or 1
            shares = [f"{k}={v / total:.0%}" for k, v in weights.items()]
            print("   card routing share:", "  ".join(shares))
        print(f"   last minute by {dimension}:", last_minute_stats(dimension))

    step("4. Recovery: incident stopped, settings restored")
    api("DELETE", "/api/chaos")
    api("PATCH", "/api/settings/gateway", {"smart_routing": False})
    api("PATCH", "/api/settings/detector", old_settings["detector"])
    if not args.keep_traffic:
        api("PATCH", "/api/settings/traffic", old_settings["traffic"])
    print("   done, see http://localhost:8080/#anomalies")


def cmd_open(args):
    url = API
    if args.page:
        url += "/#" + args.page
    webbrowser.open(url)


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="start everything")
    p.add_argument("--local", action="store_true", help="don't use Docker")
    p.add_argument("--docker", action="store_true", help="use Docker")
    p.add_argument(
        "--bi",
        action="store_true",
        help="also start Metabase on :3000 (Docker only)",
    )
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop everything")
    p.add_argument(
        "-v", "--volumes", action="store_true", help="also delete the database"
    )
    p.add_argument("--local", action="store_true")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("restart")
    p.add_argument("service", nargs="?")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("logs")
    p.add_argument("service", nargs="?")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("seed", help="generate the historical dataset")
    p.add_argument("--rows", type=int, default=500_000)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--no-anomalies", action="store_true")
    p.add_argument("--append", action="store_true")
    p.add_argument("--no-scan", action="store_true")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("traffic")
    p.add_argument(
        "action", choices=["start", "stop", "tps", "burst", "stats"]
    )
    p.add_argument("value", nargs="?")
    p.add_argument("--tps", dest="value")
    p.set_defaults(func=cmd_traffic)

    p = sub.add_parser("chaos")
    p.add_argument("action", choices=["presets", "list", "inject", "clear"])
    p.add_argument("preset", nargs="?")
    p.add_argument("--minutes", type=float, default=15)
    p.set_defaults(func=cmd_chaos)

    p = sub.add_parser("detect")
    p.add_argument("--history", action="store_true")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("anomalies")
    p.add_argument(
        "--status", default="open", choices=["open", "resolved", "all"]
    )
    p.set_defaults(func=cmd_anomalies)

    p = sub.add_parser("pay", help="send a test payment")
    p.add_argument("--merchant", default="m_001")
    p.add_argument("--amount", type=float, default=19.99)
    p.add_argument("--country", default="US")
    p.add_argument("--method", default="card")
    p.add_argument("--provider")
    p.add_argument("-n", type=int, default=1)
    p.set_defaults(func=cmd_pay)

    p = sub.add_parser("sql")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("demo", help="scripted incident")
    p.add_argument("--preset", default="provider_decline_spike")
    p.add_argument(
        "--baseline",
        type=int,
        default=120,
        help="seconds of normal traffic before the incident",
    )
    p.add_argument("--tps", type=float, default=25)
    p.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="how long to wait for detection, seconds",
    )
    p.add_argument("--keep-traffic", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("test")
    p.add_argument("pytest_args", nargs="*")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("psql")
    p.set_defaults(func=cmd_psql)

    p = sub.add_parser("open")
    p.add_argument("page", nargs="?")
    p.set_defaults(func=cmd_open)

    return parser


def main():
    if IS_WINDOWS:
        os.system("")  # turns on ANSI colors in the Windows console
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
