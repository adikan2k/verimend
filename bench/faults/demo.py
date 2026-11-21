"""The demo project and its raw data: demo/jaffle_shop and demo/raw_data.

The public jaffle shop loads its raw data as dbt seeds. Here it is loaded into a `raw`
schema of the DuckDB file and read as dbt sources, as in both public benchmarks, so the
agent can read raw data but not edit it (decision 0005).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEMO_PROJECT = REPO / "demo" / "jaffle_shop"
RAW_DATA = REPO / "demo" / "raw_data"
DB_NAME = "warehouse.duckdb"  # DuckDB names the catalog after the file: keep it

# Column types are fixed, not sniffed, so every build loads the same tables.
RAW_TABLES: dict[str, dict[str, str]] = {
    "customers": {"id": "INTEGER", "first_name": "VARCHAR", "last_name": "VARCHAR"},
    "orders": {"id": "INTEGER", "user_id": "INTEGER", "order_date": "DATE", "status": "VARCHAR"},
    "payments": {
        "id": "INTEGER",
        "order_id": "INTEGER",
        "payment_method": "VARCHAR",
        "amount": "INTEGER",
    },
}


def build_database(db_path: Path, raw_dir: Path = RAW_DATA) -> Path:
    """Create `db_path` with the raw tables loaded from `raw_dir`, replacing any old file."""
    import duckdb

    db_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (db_path, db_path.with_name(db_path.name + ".wal")):
        stale.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("create schema raw")
        for table, columns in RAW_TABLES.items():
            spec = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
            csv = (raw_dir / f"{table}.csv").as_posix()
            con.execute(
                f"create table raw.{table} as select * from read_csv('{csv}', header = true, "
                f"columns = {{{spec}}}) order by id"
            )
    finally:
        con.close()
    return db_path


def raw_fingerprint(db_path: Path) -> dict[str, str]:
    """Per raw table: a hash of its columns, their types and its rows in sorted order."""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = [
            row[0]
            for row in con.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'raw' order by table_name"
            ).fetchall()
        ]
        found: dict[str, str] = {}
        for table in tables:
            columns = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = 'raw' and table_name = ? order by ordinal_position",
                [table],
            ).fetchall()
            rows = sorted(
                repr(row) for row in con.execute(f'select * from raw."{table}"').fetchall()
            )
            digest = hashlib.sha256(repr(columns).encode())
            for row in rows:
                digest.update(row.encode())
            found[table] = digest.hexdigest()
        return found
    finally:
        con.close()
