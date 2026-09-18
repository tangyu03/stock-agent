import sys
sys.path.insert(0, '.')
from src.push.templates import render_action_summary

entries = [{
    "stock_name": "中科飞测", "stock_code": "688361", "entry_type": "价量突破",
    "trigger_price": 88.5, "current_price": 90.2, "change_pct": 2.1,
    "execution_plan": {"execution_tiers": [
        {"role": "probe", "price": 88.5, "name": "试探仓", "state": "待触发"},
        {"role": "main", "price": 89.8, "name": "主仓", "state": "待触发"},
    ]},
    "tech_signals": {"order_flow": {"drive": -2}, "rsi6": 60},
    "volume_ratio": 1.4, "turnover_rate": 5.0,
    "ma5": 87.0, "ma10": 85.0, "ma20": 83.0,
    "change_pct": 2.1,
}]
exits = [{
    "stock_name": "金海通", "stock_code": "603061", "exit_type": "破位止损",
    "stop_loss_price": 72.1, "trigger_price": 72.1, "current_price": 71.9,
    "reason": "跌破止损", "urgency": "紧急",
}]
observations = [{
    "stock_name": "蘅东光", "stock_code": "873339", "current_price": 15.3,
    "tech_signals": {"order_flow": {"drive": 1}, "rsi6": 55},
    "volume_ratio": 0.9, "turnover_rate": 2.0,
    "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
    "note": "缩量阴跌观察",
}]
out = render_action_summary(entries, exits, observations)
print(out)
print('---empty---')
print(repr(render_action_summary([], [], [])))
