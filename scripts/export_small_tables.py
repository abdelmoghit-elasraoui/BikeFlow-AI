import csv
import sqlite3

DB = r"C:\BikeFlow\data\observations.sqlite"

db = sqlite3.connect(DB)
for table in ("polls", "errors", "predictions"):
    try:
        cur = db.execute(f"SELECT * FROM {table}")
        columns = [d[0] for d in cur.description]
        path = rf"C:\BikeFlow\data\{table}.csv"
        with open(path, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(cur)
        print(table, "exported to", path)
    except Exception as exc:  # table may not exist
        print(table, "skipped:", exc)
