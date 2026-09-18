import sys
sys.path.insert(0, r'C:\Users\15831\Downloads\stock-agent2')
from src.analyzers.volume_pattern import build_volume_pattern

# 臻宝类：量比13.16 剧烈放量 vs 换手3.87% 活跃，档差2 → 应降级待定
r = build_volume_pattern({
    "change_pct": 1.0,
    "volume_ratio": 13.16,
    "turnover_rate": 3.87,
    "current_price": 100.0,
    "ma5": 101.0, "ma10": 102.0, "ma20": 103.0,
    "prior_high": 104.0, "recent_high": 104.0,
    "gain_20d": 0.05, "ma20_falling": False,
    "tech_signals": {"order_flow": {"available": True, "outer_volume": 1200, "inner_volume": 800, "imbalance_pct": 20.0}},
})
print("pattern:", r["pattern"], "| star:", r["star"], "| gear_gap:", r["gear_gap"])
print("note:", r["volume_caliber_conflict_note"])
print("short:", r["summary_short"])
assert r["pattern"] == "量能口径待定"
assert r["star"] == 1
assert r["gear_gap"] == 2

# 飞荣达验收：量比0.89 缩量 vs 换手6.55% 活跃，档差 -2 → 不得降级
r2 = build_volume_pattern({
    "change_pct": -2.0,
    "volume_ratio": 0.89,
    "turnover_rate": 6.55,
    "current_price": 30.0,
    "ma5": 31.0, "ma10": 32.0, "ma20": 33.0,
    "prior_high": 34.0, "recent_high": 34.0,
    "gain_20d": 0.02, "ma20_falling": True,
    "tech_signals": {"order_flow": {"available": True, "outer_volume": 1000, "inner_volume": 1500, "imbalance_pct": -20.0}},
})
print("r2 pattern:", r2["pattern"], "| gear_gap:", r2["gear_gap"])
assert r2["pattern"] != "量能口径待定"

# 档差不足2（明显放量3 vs 活跃2=1）不得降级
r3 = build_volume_pattern({
    "change_pct": 1.0,
    "volume_ratio": 3.0,
    "turnover_rate": 5.0,
    "current_price": 100.0,
    "ma5": 99.0, "ma10": 98.0, "ma20": 97.0,
    "prior_high": 110.0, "recent_high": 110.0,
    "gain_20d": 0.05, "ma20_falling": False,
    "tech_signals": {"order_flow": {"available": True, "outer_volume": 1200, "inner_volume": 800, "imbalance_pct": 20.0}},
})
print("r3 pattern:", r3["pattern"], "| gear_gap:", r3["gear_gap"])
assert r3["gear_gap"] == 1
assert r3["pattern"] != "量能口径待定"
print("OK")
