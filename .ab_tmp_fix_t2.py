# -*- coding: utf-8 -*-
p = r"tests/test_timing_engine.py"
s = open(p, encoding="utf-8").read()
old = '''            "tech_signals": {
                "order_flow": {"outer_volume": 60, "inner_volume": 40, "available": True},
                "volume_snapshot": {"volume_ratio": 2.5, "turnover_rate": 5.0},
            },'''
new = '''            "tech_signals": {
                "order_flow": {"imbalance_pct": 20.0, "available": True},
                "volume_snapshot": {"volume_ratio": 2.5, "turnover_rate": 5.0},
            },'''
assert old in s, "anchor1"
s = s.replace(old, new, 1)
old2 = '''        tech["tech_signals"]["order_flow"] = {
            "outer_volume": 30, "inner_volume": 70, "available": True}'''
new2 = '''        tech["tech_signals"]["order_flow"] = {
            "imbalance_pct": -20.0, "available": True}'''
assert old2 in s, "anchor2"
s = s.replace(old2, new2, 1)
open(p, "w", encoding="utf-8").write(s)
print("OK")
