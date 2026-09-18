import sys
sys.path.insert(0, '.')
from src.push.templates import render_action_summary
observations = [{
    "stock_name": "蘅东光", "stock_code": "873339", "current_price": 15.3,
    "change_pct": -1.2,
    "tech_signals": {"order_flow": {"available": True, "imbalance_pct": -3.0}, "rsi6": 55},
    "volume_ratio": 0.9, "turnover_rate": 2.0,
    "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
    "note": "缩量阴跌观察",
}]
print(render_action_summary([], [], observations))
