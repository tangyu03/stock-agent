import sqlite3

con = sqlite3.connect("data/stock_agent.db")
con.row_factory = sqlite3.Row
cur = con.cursor()
rows = cur.execute(
    "select * from observation_intercepts where track_date = ? "
    "order by created_at desc",
    ("2026-10-16",),
).fetchall()
print("count", len(rows))
for row in rows:
    print(dict(row))
