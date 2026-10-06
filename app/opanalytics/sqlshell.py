"""Small SQL shell for local mode (the portable PostgreSQL has no psql).

End statements with ';'. Commands: \\q - quit, \\dt - tables,
\\d <table> - describe a table.
"""

import os

import psycopg

from .db import DATABASE_URL

LIST_TABLES = """
    SELECT table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY 1;
"""

DESCRIBE_TABLE = """
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_name = '{}'
    ORDER BY ordinal_position;
"""


def show(cur, limit=200):
    if cur.description is None:
        print(cur.statusmessage)
        return

    cols = [d.name for d in cur.description]
    rows = []
    for row in cur.fetchmany(limit):
        rows.append(["NULL" if v is None else str(v) for v in row])

    widths = []
    for i, col in enumerate(cols):
        widths.append(max([len(col)] + [len(r[i]) for r in rows]))

    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(v.ljust(w) for v, w in zip(r, widths)))
    more = "+" if len(rows) == limit else ""
    print(f"({len(rows)}{more} rows)")


def main():
    url = os.environ.get("DATABASE_URL", DATABASE_URL)
    print(f"Connected to {url.split('@')[-1]}")
    print("End statements with ';'. \\q to quit, \\dt for tables.")

    with psycopg.connect(url, autocommit=True) as conn:
        buf = ""
        while True:
            try:
                line = input("...> " if buf else "opa=> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return

            line = line.strip()
            if not buf:
                if line in ("\\q", "exit", "quit"):
                    return
                if line == "\\dt":
                    line = LIST_TABLES
                elif line.startswith("\\d "):
                    table = line.split()[1].replace("'", "")
                    line = DESCRIBE_TABLE.format(table)

            buf += line + "\n"
            if not buf.rstrip().endswith(";"):
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(buf)
                    show(cur)
            except psycopg.Error as e:
                print("ERROR:", str(e).strip())
            buf = ""


if __name__ == "__main__":
    main()
