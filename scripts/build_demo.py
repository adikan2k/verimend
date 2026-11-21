"""Build demo/jaffle_shop/warehouse.duckdb from demo/raw_data, then print its fingerprint.

    uv run python scripts/build_demo.py

The database is built, not committed. The same CSV files always give the same tables.
"""

import sys

from bench.faults.demo import DB_NAME, DEMO_PROJECT, build_database, raw_fingerprint


def main() -> int:
    db = build_database(DEMO_PROJECT / DB_NAME)
    for table, digest in raw_fingerprint(db).items():
        print(f"raw.{table:10} {digest[:16]}")
    print(f"Wrote {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
