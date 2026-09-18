import sys, json
sys.path.insert(0, '.')
from src.analyzers.volume_pattern import build_volume_pattern
obs = {
    "stock_name": "蘅东光", "stock_code": "873339", "current_price": 15.3,
    "change_pct": -1.2,
    "tech_signals": {"order_flow": {"drive": 1}, "rsi6": 55},
    "volume_ratio": 0.9, "turnover_rate": 2.0,
    "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
    "note": "缩量阴跌观察",
}
r = build_volume_pattern(obs)
for k in ("available","pattern","confirms","confirm_target","summary_short","star","drive_label","position","near_high_pct"):
    print(k, "=", r.get(k))
