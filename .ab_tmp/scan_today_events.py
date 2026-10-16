import sqlite3

con = sqlite3.connect("data/stock_agent.db")
con.row_factory = sqlite3.Row
cur = con.cursor()
rows = cur.execute(
    "select * from signal_events where updated_at like ? "
    "order by updated_at desc",
    ("2026-10-16T14%",),
).fetchall()
print("count", len(rows))
for row in rows:
    print(dict(row))
