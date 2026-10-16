import sqlite3
import json

con = sqlite3.connect("data/stock_agent.db")
value = con.execute(
    "select cache_value from data_cache where cache_key = ?",
    ("last_intraday_report_snapshot",),
).fetchone()[0]
data = json.loads(value)
print("time", data.get("pushed_at"), "mode", data.get("market_mode"), "score", data.get("market_score"))
print(json.dumps(data, ensure_ascii=False, indent=2))
