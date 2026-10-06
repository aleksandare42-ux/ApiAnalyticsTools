"""Integration with BI tools: Tableau, Power BI, Excel.

Everything here is read-only and works with a fixed list of datasets
(the BI views plus a few small tables):

  /api/bi/info                       connection details and datasets
  /api/bi/check                      can the read-only BI user read them?
  /api/bi/csv/{dataset}.csv          full dataset as CSV (Power BI "Web")
  /api/bi/tableau/{dataset}.tds      Tableau data source (live PostgreSQL)
  /api/bi/tableau/payops.hyper       Tableau extract, opens in Tableau Public
  /api/bi/powerbi/postgres.pbids     Power BI connection file
  /api/bi/powerbi/{dataset}.pbids    Power BI "Web" connection to the CSV
  /api/bi/powerbi/queries.pq         Power Query (M) for all datasets
  /api/bi/powerbi/measures.dax       DAX measures for the model
  /odata/...                         OData v4 feed (Power BI, Excel, Tableau)
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode, urlparse
from xml.sax.saxutils import escape

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

from .db import DATABASE_URL

router = APIRouter()

BI_USER = "analyst"
BI_PASSWORD = "analyst"
ODATA_PAGE_SIZE = 50_000


class Dataset:
    def __init__(self, name, table, entity, keys, description):
        self.name = name
        self.table = table
        self.entity = entity
        self.keys = keys
        self.description = description


DATASETS = {
    d.name: d
    for d in [
        Dataset(
            "transactions",
            "v_transactions",
            "Transactions",
            ["transaction_id"],
            "One row per payment attempt, with merchant and provider "
            "names and 0/1 flags (is_approved, is_declined...)",
        ),
        Dataset(
            "hourly_provider",
            "v_hourly_provider",
            "HourlyProvider",
            ["hour", "provider"],
            "Hourly counts, latency and volume per provider",
        ),
        Dataset(
            "daily_kpis",
            "v_daily_kpis",
            "DailyKpis",
            ["day"],
            "Daily KPIs: success / decline / error / refund rate",
        ),
        Dataset(
            "daily_merchant",
            "v_daily_merchant",
            "DailyMerchant",
            ["day", "merchant_id"],
            "Daily refunds and volume per merchant",
        ),
        Dataset(
            "country_provider",
            "v_country_provider",
            "CountryProvider",
            ["country", "provider"],
            "Approval rate for every country and provider pair",
        ),
        Dataset(
            "anomalies",
            "anomalies",
            "Anomalies",
            ["anomaly_id"],
            "Incidents found by the detector",
        ),
        Dataset(
            "merchants",
            "merchants",
            "Merchants",
            ["merchant_id"],
            "Merchant reference table",
        ),
        Dataset(
            "providers",
            "providers",
            "Providers",
            ["provider_id"],
            "Payment provider reference table",
        ),
        Dataset(
            "ground_truth",
            "seed_ground_truth",
            "GroundTruth",
            ["id"],
            "Anomalies injected into the historical data (answers)",
        ),
    ]
}
DATASETS_BY_ENTITY = {d.entity: d for d in DATASETS.values()}

# postgres type -> (OData type, Hyper type)
TYPES = {
    "uuid": ("Edm.Guid", "text"),
    "text": ("Edm.String", "text"),
    "character": ("Edm.String", "text"),
    "character varying": ("Edm.String", "text"),
    "smallint": ("Edm.Int16", "smallint"),
    "integer": ("Edm.Int32", "integer"),
    "bigint": ("Edm.Int64", "bigint"),
    "double precision": ("Edm.Double", "double precision"),
    "real": ("Edm.Double", "double precision"),
    "boolean": ("Edm.Boolean", "bool"),
    "date": ("Edm.Date", "date"),
    "timestamp with time zone": ("Edm.DateTimeOffset", "timestamptz"),
    # arrays are sent as a comma separated string
    "ARRAY": ("Edm.String", "text"),
}

COLUMNS_SQL = """
SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""


def connection_info():
    """Where BI tools running on this computer should connect to."""
    url = urlparse(DATABASE_URL)
    if url.hostname in ("127.0.0.1", "localhost"):
        port = url.port or 5432
    else:
        # docker: postgres is published on the host
        port = int(os.environ.get("POSTGRES_PUBLIC_PORT", 5432))
    return {
        "host": "localhost",
        "port": port,
        "database": url.path.lstrip("/") or "opanalytics",
        "user": BI_USER,
        "password": BI_PASSWORD,
        "schema": "public",
    }


def bi_user_url():
    url = urlparse(DATABASE_URL)
    host = url.hostname or "127.0.0.1"
    port = url.port or 5432
    database = url.path.lstrip("/")
    return f"postgresql://{BI_USER}:{BI_PASSWORD}@{host}:{port}/{database}"


def get_dataset(name):
    if name not in DATASETS:
        raise HTTPException(
            404, f"unknown dataset, use one of {list(DATASETS)}"
        )
    return DATASETS[name]


# Column metadata is read once, the views don't change at runtime.
_columns_cache = {}


async def get_columns(conn, dataset):
    if not _columns_cache:
        cur = await conn.execute(COLUMNS_SQL)
        for table, column, data_type, precision, scale in await cur.fetchall():
            _columns_cache.setdefault(table, []).append(
                {
                    "name": column,
                    "type": data_type,
                    "precision": precision,
                    "scale": scale,
                }
            )
    return _columns_cache.get(dataset.table, [])


def select_list(columns, readable_bool=False):
    parts = []
    for col in columns:
        if readable_bool and col["type"] == "boolean":
            # CSV readers (Power BI, Excel) understand true/false, not t/f
            parts.append(f"{col['name']}::text AS {col['name']}")
        elif col["type"] == "ARRAY":
            parts.append(
                f"array_to_string({col['name']}, ',') AS {col['name']}"
            )
        else:
            parts.append(col["name"])
    return ", ".join(parts)


def odata_type(col):
    if col["type"] == "numeric":
        # numeric(14,2) keeps its scale, unconstrained numeric becomes double
        if col["precision"] is not None:
            return "Edm.Decimal"
        return "Edm.Double"
    return TYPES.get(col["type"], ("Edm.String", "text"))[0]


def hyper_type(col):
    if col["type"] == "numeric":
        if col["precision"] is not None:
            return f"numeric({col['precision']}, {col['scale'] or 0})"
        return "double precision"
    return TYPES.get(col["type"], ("Edm.String", "text"))[1]


def download(content, filename, media_type="application/octet-stream"):
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content, media_type=media_type, headers=headers)


# Info and check


@router.get("/api/bi/info")
async def info(request: Request):
    base = str(request.base_url).rstrip("/")
    datasets = []
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        for dataset in DATASETS.values():
            columns = await get_columns(conn, dataset)
            cur = await conn.execute(f"SELECT count(*) FROM {dataset.table}")
            rows = (await cur.fetchone())[0]
            datasets.append(
                {
                    "name": dataset.name,
                    "table": dataset.table,
                    "entity": dataset.entity,
                    "description": dataset.description,
                    "rows": rows,
                    "columns": [c["name"] for c in columns],
                    "csv_url": f"{base}/api/bi/csv/{dataset.name}.csv",
                }
            )
    return {
        "connection": connection_info(),
        "datasets": datasets,
        "odata_url": f"{base}/odata",
        "hyper_available": hyper_available(),
    }


@router.get("/api/bi/check")
async def check():
    """Connect the same way a BI tool would and read every dataset."""
    started = time.perf_counter()
    try:
        conn = await psycopg.AsyncConnection.connect(
            bi_user_url(), connect_timeout=5
        )
    except psycopg.Error as e:
        return {"ok": False, "error": str(e).strip(), "datasets": []}

    results = []
    async with conn:
        for dataset in DATASETS.values():
            try:
                cur = await conn.execute(
                    f"SELECT count(*) FROM {dataset.table}"
                )
                rows = (await cur.fetchone())[0]
                results.append(
                    {"name": dataset.name, "ok": True, "rows": rows}
                )
            except psycopg.Error as e:
                await conn.rollback()
                results.append(
                    {
                        "name": dataset.name,
                        "ok": False,
                        "error": str(e).strip(),
                    }
                )
    return {
        "ok": all(r["ok"] for r in results),
        "user": BI_USER,
        "ms": round((time.perf_counter() - started) * 1000),
        "datasets": results,
    }


# CSV


@router.get("/api/bi/csv/{name}.csv")
async def dataset_csv(name: str):
    dataset = get_dataset(name)

    async def stream():
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            columns = await get_columns(conn, dataset)
            fields = select_list(columns, readable_bool=True)
            sql = (
                f"COPY (SELECT {fields} FROM {dataset.table}) "
                "TO STDOUT WITH (FORMAT csv, HEADER)"
            )
            async with conn.cursor().copy(sql) as copy:
                async for chunk in copy:
                    yield bytes(chunk)

    headers = {"Content-Disposition": f'attachment; filename="{name}.csv"'}
    return StreamingResponse(stream(), media_type="text/csv", headers=headers)


# Tableau

TDS_TEMPLATE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='PayOps {name}' inline='true' version='18.1'
    xmlns:user='http://www.tableausoftware.com/xml/user'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='{host}' name='{conn_name}'>
        <connection authentication='username-password' class='postgres'
            dbname='{database}' odbc-native-protocol='yes' port='{port}'
            server='{host}' sslmode='' username='{user}' />
      </named-connection>
    </named-connections>
    <relation connection='{conn_name}' name='{table}'
        table='[public].[{table}]' type='table' />
  </connection>
</datasource>
"""


@router.get("/api/bi/tableau/{name}.tds")
async def tableau_tds(name: str):
    dataset = get_dataset(name)
    conn = connection_info()
    content = TDS_TEMPLATE.format(
        name=escape(dataset.name),
        conn_name="postgres." + uuid.uuid4().hex[:12],
        table=dataset.table,
        **conn,
    )
    return download(content, f"payops_{name}.tds", "application/xml")


def hyper_available():
    try:
        import tableauhyperapi  # noqa: F401
    except ImportError:
        return False
    return True


def build_hyper(path, tables):
    """tables: list of (table name, columns, csv file)."""
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        Telemetry,
        escape_string_literal,
    )

    telemetry = Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU
    with HyperProcess(telemetry=telemetry) as hyper:
        with Connection(
            hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE
        ) as conn:
            conn.execute_command('CREATE SCHEMA IF NOT EXISTS "Extract"')
            for name, columns, csv_path in tables:
                column_sql = ", ".join(
                    f'"{c["name"]}" {hyper_type(c)}' for c in columns
                )
                conn.execute_command(
                    f'CREATE TABLE "Extract"."{name}" ({column_sql})'
                )
                conn.execute_command(
                    f'COPY "Extract"."{name}" '
                    f"FROM {escape_string_literal(csv_path)} "
                    "WITH (format csv, header true, null '')"
                )


@router.get("/api/bi/tableau/payops.hyper")
async def tableau_hyper(datasets: str = None):
    """All datasets (or ?datasets=a,b) in one Tableau extract file."""
    if not hyper_available():
        raise HTTPException(
            501,
            "Tableau Hyper API is not installed. Run: "
            ".venv\\Scripts\\pip install -r requirements-bi.txt "
            "and restart the dashboard",
        )
    names = datasets.split(",") if datasets else list(DATASETS)
    selected = [get_dataset(name) for name in names]

    workdir = tempfile.mkdtemp(prefix="payops_hyper_")
    tables = []
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        for dataset in selected:
            columns = await get_columns(conn, dataset)
            csv_path = os.path.join(workdir, dataset.name + ".csv")
            sql = (
                f"COPY (SELECT {select_list(columns)} FROM {dataset.table}) "
                "TO STDOUT WITH (FORMAT csv, HEADER)"
            )
            with open(csv_path, "wb") as f:
                async with conn.cursor().copy(sql) as copy:
                    async for chunk in copy:
                        f.write(chunk)
            tables.append((dataset.name, columns, csv_path))

    hyper_path = os.path.join(workdir, "payops.hyper")
    try:
        await asyncio.to_thread(build_hyper, hyper_path, tables)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return FileResponse(
        hyper_path,
        filename="payops.hyper",
        media_type="application/octet-stream",
        background=BackgroundTask(shutil.rmtree, workdir, True),
    )


# Power BI


def pbids(details, mode=None):
    connection = {"details": details, "options": {}}
    if mode:
        connection["mode"] = mode
    return json.dumps(
        {"version": "0.1", "connections": [connection]}, indent=2
    )


@router.get("/api/bi/powerbi/postgres.pbids")
async def powerbi_postgres(mode: str = "Import"):
    if mode not in ("Import", "DirectQuery"):
        raise HTTPException(422, "mode must be Import or DirectQuery")
    conn = connection_info()
    content = pbids(
        {
            "protocol": "postgresql",
            "address": {
                "server": f"{conn['host']}:{conn['port']}",
                "database": conn["database"],
            },
        },
        mode,
    )
    filename = f"payops_postgres_{mode.lower()}.pbids"
    return download(content, filename, "application/json")


@router.get("/api/bi/powerbi/{name}.pbids")
async def powerbi_web(name: str, request: Request):
    get_dataset(name)
    url = f"{str(request.base_url).rstrip('/')}/api/bi/csv/{name}.csv"
    content = pbids({"protocol": "http", "address": {"url": url}})
    return download(content, f"payops_{name}_web.pbids", "application/json")


M_POSTGRES = (
    """    {name} = Source{{[Schema="public", Item="{table}"]}}[Data]"""
)

M_WEB = """// {name}: from the CSV feed, no database driver needed
let
    Source = Csv.Document(
        Web.Contents("{url}"),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true])
in
    Promoted
"""


@router.get("/api/bi/powerbi/queries.pq")
async def powerbi_queries(request: Request):
    base = str(request.base_url).rstrip("/")
    conn = connection_info()
    lines = [
        "// Power Query (M) for Payment Ops Analytics.",
        "// Power BI: Home > Transform data > New source > Blank query >",
        "// Advanced editor, paste one of the queries below.",
        "",
        "// 1. All datasets from PostgreSQL (one query per table is the",
        "//    usual way, this one shows the navigation step for each).",
        "let",
        f'    Source = PostgreSQL.Database("{conn["host"]}:{conn["port"]}", '
        f'"{conn["database"]}"),',
    ]
    steps = [
        M_POSTGRES.format(name=d.entity, table=d.table)
        for d in DATASETS.values()
    ]
    lines.append(",\n".join(steps))
    lines += ["in", "    Transactions", ""]
    lines.append("// 2. The same data over HTTP, one query per dataset.")
    lines.append("")
    for dataset in DATASETS.values():
        url = f"{base}/api/bi/csv/{dataset.name}.csv"
        lines.append(M_WEB.format(name=dataset.entity, url=url))
    lines.append("// 3. OData feed with all datasets:")
    lines.append(f'// = OData.Feed("{base}/odata")')
    return PlainTextResponse("\n".join(lines))


DAX_MEASURES = """// DAX measures for the Transactions table (v_transactions).
// Modeling > New measure, paste one measure at a time.
// If you named the table differently, replace 'Transactions'.

Transactions = COUNTROWS ( Transactions )

Approved = SUM ( Transactions[is_approved] )

Approval Rate = DIVIDE ( [Approved], [Transactions] )

Decline Rate = DIVIDE ( SUM ( Transactions[is_declined] ), [Transactions] )

Error Rate = DIVIDE ( SUM ( Transactions[is_error] ), [Transactions] )

Refund Rate = DIVIDE ( SUM ( Transactions[is_refunded] ), [Approved] )

Approved Volume USD =
CALCULATE (
    SUM ( Transactions[amount_usd] ),
    Transactions[status] = "approved"
)

Avg Processing ms = AVERAGE ( Transactions[processing_time_ms] )

P95 Processing ms = PERCENTILE.INC ( Transactions[processing_time_ms], 0.95 )

// Payment-level conversion: a payment counts once even if it was retried
// on another provider (cascading).
Conversion =
DIVIDE (
    CALCULATE (
        DISTINCTCOUNT ( Transactions[payment_id] ),
        Transactions[status] = "approved"
    ),
    DISTINCTCOUNT ( Transactions[payment_id] )
)

// Approval rate over the whole history, ignoring the date filter.
// Useful as a baseline line on charts.
Approval Rate Baseline =
CALCULATE ( [Approval Rate], REMOVEFILTERS ( Transactions[created_at] ) )

Approval Rate vs Baseline (pp) =
( [Approval Rate] - [Approval Rate Baseline] ) * 100

// Flags a provider / country / merchant whose approval rate is more than
// 10pp under its baseline - put it on a table visual to find incidents.
Approval Drop Flag =
IF ( [Transactions] >= 30 && [Approval Rate vs Baseline (pp)] <= -10, 1, 0 )
"""


@router.get("/api/bi/powerbi/measures.dax")
async def powerbi_measures():
    return PlainTextResponse(DAX_MEASURES)


# OData v4


def odata_headers():
    return {"OData-Version": "4.0"}


def odata_url(request):
    return str(request.base_url).rstrip("/") + "/odata"


@router.get("/odata")
@router.get("/odata/")
async def odata_service(request: Request):
    base = odata_url(request)
    value = [
        {"name": d.entity, "kind": "EntitySet", "url": d.entity}
        for d in DATASETS.values()
    ]
    return JSONResponse(
        {"@odata.context": f"{base}/$metadata", "value": value},
        headers=odata_headers(),
    )


@router.get("/odata/$metadata")
async def odata_metadata():
    entity_types = []
    entity_sets = []
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        for dataset in DATASETS.values():
            columns = await get_columns(conn, dataset)
            keys = "".join(f'<PropertyRef Name="{k}"/>' for k in dataset.keys)
            props = []
            for col in columns:
                edm = odata_type(col)
                attrs = f'Name="{col["name"]}" Type="{edm}"'
                if col["name"] in dataset.keys:
                    attrs += ' Nullable="false"'
                if edm == "Edm.Decimal":
                    attrs += (
                        f' Precision="{col["precision"]}"'
                        f' Scale="{col["scale"] or 0}"'
                    )
                if edm == "Edm.DateTimeOffset":
                    attrs += ' Precision="6"'
                props.append(f"<Property {attrs}/>")
            entity_types.append(
                f'<EntityType Name="{dataset.entity}Row">'
                f"<Key>{keys}</Key>{''.join(props)}</EntityType>"
            )
            entity_sets.append(
                f'<EntitySet Name="{dataset.entity}" '
                f'EntityType="PayOps.{dataset.entity}Row"/>'
            )

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx Version="4.0" '
        'xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
        "<edmx:DataServices>"
        '<Schema Namespace="PayOps" '
        'xmlns="http://docs.oasis-open.org/odata/ns/edm">'
        + "".join(entity_types)
        + '<EntityContainer Name="Container">'
        + "".join(entity_sets)
        + "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    )
    return Response(xml, media_type="application/xml", headers=odata_headers())


FILTER_OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
}
FILTER_PART = re.compile(
    r"^\s*(\w+)\s+(eq|ne|gt|ge|lt|le)\s+"
    r"('(?:[^']|'')*'|null|true|false|[-+]?\d+(?:\.\d+)?"
    r"|\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?)\s*$"
)


def parse_filter(text, column_names):
    """Translate a simple OData $filter into SQL.

    Only `column op value` conditions joined with `and` are supported,
    which is what Power BI and Excel send for basic filters.
    """
    conditions = []
    params = []
    for part in re.split(r"\s+and\s+", text.strip()):
        part = part.strip()
        while part.startswith("(") and part.endswith(")"):
            part = part[1:-1].strip()
        match = FILTER_PART.match(part)
        if not match:
            raise HTTPException(
                400, f"unsupported $filter expression: {part!r}"
            )
        column, op, value = match.groups()
        if column not in column_names:
            raise HTTPException(400, f"unknown column in $filter: {column}")

        if value == "null":
            if op not in ("eq", "ne"):
                raise HTTPException(400, "null can only be used with eq/ne")
            check = "IS NULL" if op == "eq" else "IS NOT NULL"
            conditions.append(f"{column} {check}")
            continue

        if value.startswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value in ("true", "false"):
            value = value == "true"
        elif re.fullmatch(r"[-+]?\d+(\.\d+)?", value):
            value = Decimal(value)
        conditions.append(f"{column} {FILTER_OPERATORS[op]} %s")
        params.append(value)
    return " AND ".join(conditions), params


def odata_value(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def query_entity(dataset, params, count_only=False):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        columns = await get_columns(conn, dataset)
        names = [c["name"] for c in columns]

        where = "true"
        sql_params = []
        if params.get("$filter"):
            where, sql_params = parse_filter(params["$filter"], names)

        cur = await conn.execute(
            f"SELECT count(*) FROM {dataset.table} WHERE {where}", sql_params
        )
        total = (await cur.fetchone())[0]
        if count_only:
            return total, None, None

        selected = columns
        if params.get("$select"):
            wanted = [s.strip() for s in params["$select"].split(",")]
            unknown = set(wanted) - set(names)
            if unknown:
                raise HTTPException(400, f"unknown columns: {sorted(unknown)}")
            selected = [c for c in columns if c["name"] in wanted]

        skip = int(params.get("$skiptoken") or params.get("$skip") or 0)
        top = int(params.get("$top") or 0)
        page = ODATA_PAGE_SIZE
        if top:
            page = min(page, top)

        order_by = ", ".join(dataset.keys)
        cur = await conn.execute(
            f"SELECT {select_list(selected)} FROM {dataset.table} "
            f"WHERE {where} ORDER BY {order_by} "
            f"LIMIT {page} OFFSET {skip}",
            sql_params,
        )
        rows = await cur.fetchall()
        names = [c["name"] for c in selected]
        values = [
            {n: odata_value(v) for n, v in zip(names, row)} for row in rows
        ]

    next_skip = None
    reached_top = top and skip + len(rows) >= top
    if len(rows) == page and not reached_top and skip + page < total:
        next_skip = skip + page
    return total, values, next_skip


@router.get("/odata/{entity}/$count")
async def odata_count(entity: str, request: Request):
    dataset = DATASETS_BY_ENTITY.get(entity)
    if not dataset:
        raise HTTPException(404, f"unknown entity set {entity}")
    total, _, _ = await query_entity(
        dataset, dict(request.query_params), count_only=True
    )
    return PlainTextResponse(str(total), headers=odata_headers())


@router.get("/odata/{entity}")
async def odata_entity(entity: str, request: Request):
    dataset = DATASETS_BY_ENTITY.get(entity)
    if not dataset:
        raise HTTPException(404, f"unknown entity set {entity}")

    params = dict(request.query_params)
    total, values, next_skip = await query_entity(dataset, params)

    base = odata_url(request)
    body = {"@odata.context": f"{base}/$metadata#{entity}"}
    if params.get("$count") == "true":
        body["@odata.count"] = total
    body["value"] = values
    if next_skip is not None:
        keep = {
            k: v
            for k, v in params.items()
            if k in ("$filter", "$select", "$count")
        }
        keep["$skiptoken"] = str(next_skip)
        query = urlencode(keep, safe="$,")
        body["@odata.nextLink"] = f"{base}/{entity}?{query}"

    headers = odata_headers()
    headers["Content-Type"] = "application/json;odata.metadata=minimal"
    return JSONResponse(body, headers=headers)
