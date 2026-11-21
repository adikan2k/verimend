"""Load raw_data/*.csv into the raw schema of warehouse.duckdb, as the ingestion job does.

python scripts/load_raw.py
"""

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW_TABLES = {
    "customers": {"id": "INTEGER", "first_name": "VARCHAR", "last_name": "VARCHAR"},
    "orders": {"id": "INTEGER", "user_id": "INTEGER", "order_date": "DATE", "status": "VARCHAR"},
    "payments": {
        "id": "INTEGER",
        "order_id": "INTEGER",
        "payment_method": "VARCHAR",
        "amount": "INTEGER",
    },
}

db = ROOT / "warehouse.duckdb"
db.unlink(missing_ok=True)
con = duckdb.connect(str(db))
con.execute("create schema raw")
for table, columns in RAW_TABLES.items():
    spec = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
    csv = (ROOT / "raw_data" / f"{table}.csv").as_posix()
    con.execute(
        f"create table raw.{table} as select * from read_csv('{csv}', header = true, "
        f"columns = {{{spec}}}) order by id"
    )
con.close()
print(f"Loaded {len(RAW_TABLES)} raw tables into {db.name}")
