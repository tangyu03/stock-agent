import sys
sys.path.insert(0, '.')
from src.push.templates import render_action_summary
# 观察票带完整分型输入，验证 confirm 待验条件
observations = [{
    "stock_name": "蘅东光", "stock_code": "873339", "current_price": 15.3,
    "change_pct": -1.2,
    "tech_signals": {"order_flow": {"drive": 1}, "rsi6": 55},
    "volume_ratio": 0.9, "turnover_rate": 2.0,
    "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
    "note": "缩量阴跌观察",
}]
print(render_action_summary([], [], observations))
