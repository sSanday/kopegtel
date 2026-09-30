#!/usr/bin/env python3
import argparse
import os
import re
import sqlite3
import sys

try:
    import psycopg
except ImportError:
    print("Install psycopg[binary] before running this command.", file=sys.stderr)
    raise


def postgres_type(declaration):
    value = declaration.upper()
    if "INT" in value:
        return "BIGINT"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in value:
        return "BYTEA"
    if "BOOL" in value:
        return "BOOLEAN"
    if "DATE" in value or "TIME" in value:
        return "TIMESTAMP"
    return "TEXT"


def table_definition(sql):
    body = sql[sql.find("(") + 1 : sql.rfind(")")]
    columns = []
    for item in body.splitlines():
        item = item.strip().rstrip(",")
        if not item or item.upper().startswith(
            ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")
        ):
            continue
        match = re.match(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+)', item)
        if not match:
            continue
        name, declaration = match.groups()
        if "PRIMARY KEY" in declaration.upper() and "INT" in declaration.upper():
            columns.append(f'"{name}" BIGSERIAL PRIMARY KEY')
        else:
            declaration = re.sub(r"DEFAULT\s+[^\s,]+", "", declaration, flags=re.I)
            declaration = re.sub(r"NOT NULL", "NOT NULL", declaration, flags=re.I)
            columns.append(f'"{name}" {postgres_type(declaration)}')
    return columns


def migrate(source, target):
    sqlite_conn = sqlite3.connect(source)
    sqlite_conn.row_factory = sqlite3.Row
    tables = [
        row[0]
        for row in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    with psycopg.connect(target) as pg_conn:
        with pg_conn.cursor() as pg:
            for table in tables:
                schema = sqlite_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                columns = table_definition(schema)
                if not columns:
                    continue
                # pg.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
                # pg.execute(f'CREATE TABLE "{table}" ({", ".join(columns)})')
                rows = sqlite_conn.execute(f'SELECT * FROM "{table}"').fetchall()
                names = [
                    description[0]
                    for description in sqlite_conn.execute(
                        f'SELECT * FROM "{table}"'
                    ).description
                ]
                if rows:
                    fields = ", ".join(f'"{name}"' for name in names)
                    placeholders = ", ".join("%s" for _ in names)
                    pg.executemany(
                        f'INSERT INTO "{table}" ({fields}) VALUES ({placeholders})',
                        [tuple(row[name] for name in names) for row in rows],
                    )
                print(f"migrated {table}: {len(rows)} rows")
    sqlite_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate NMS SQLite database to PostgreSQL."
    )
    parser.add_argument("--sqlite", default=os.environ.get("NMS_DB_PATH", "network.db"))
    parser.add_argument("--postgres", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.postgres:
        parser.error("--postgres or DATABASE_URL is required")
    migrate(args.sqlite, args.postgres)
