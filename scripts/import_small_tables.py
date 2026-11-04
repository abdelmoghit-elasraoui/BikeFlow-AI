"""Import small index tables (polls, errors, predictions) into a fresh DB.

Usage on the new host:
    python3 scripts/import_small_tables.py /path/to/observations.sqlite /path/to/csv_dir
"""

import csv
import sqlite3
import sys

db_path = sys.argv[1]
csv_dir = sys.argv[2].rstrip("/")
db = sqlite3.connect(db_path)

for table in ("polls", "errors", "predictions"):
    path = f"{csv_dir}/{table}.csv"
    try:
        with open(path, newline="", encoding="utf-8") as stream:
            reader = csv.reader(stream)
            columns = next(reader)
            rows = list(reader)
    except FileNotFoundError:
        print(f"{table}: no csv, skipped")
        continue
    placeholders = ",".join("?" * len(columns))
    db.executemany(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", rows
    )
    db.commit()
    print(f"{table}: imported {len(rows)} rows")
