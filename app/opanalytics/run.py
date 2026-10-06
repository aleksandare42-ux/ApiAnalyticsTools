"""Run one service: python -m opanalytics.run <service> [--port N]

Used in local mode. On Windows the async psycopg driver needs a selector
event loop, so we start uvicorn.Server ourselves instead of using the CLI.
"""

import argparse
import asyncio
import sys

import uvicorn

SERVICES = {
    "gateway": ("opanalytics.services.gateway:app", 8000),
    "providers": ("opanalytics.services.providers:app", 8001),
    "traffic": ("opanalytics.services.traffic:app", 8002),
    "detector": ("opanalytics.services.detector:app", 8003),
    "dashboard": ("opanalytics.services.dashboard:app", 8080),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("service", choices=SERVICES)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    target, default_port = SERVICES[args.service]
    config = uvicorn.Config(
        target,
        host=args.host,
        port=args.port or default_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
