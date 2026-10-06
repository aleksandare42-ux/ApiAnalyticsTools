"""Create the database for local mode.

The portable PostgreSQL build has no psql, so opa.py runs this script
right after initdb / pg_ctl start.

    python -m opanalytics.createdb <port> <path to roles.sql>
"""

import sys
from pathlib import Path

import psycopg


def main():
    port = sys.argv[1]
    roles_sql = Path(sys.argv[2]).read_text(encoding="utf-8")
    server_url = f"postgresql://opa@127.0.0.1:{port}"

    with psycopg.connect(server_url + "/postgres", autocommit=True) as conn:
        conn.execute("ALTER ROLE opa PASSWORD 'opa'")
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'opanalytics'"
        ).fetchone()
        if exists:
            return
        conn.execute("CREATE DATABASE opanalytics")

    with psycopg.connect(server_url + "/opanalytics", autocommit=True) as conn:
        conn.execute(roles_sql)
    print("  database opanalytics created")


if __name__ == "__main__":
    main()
