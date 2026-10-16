# -*- coding: utf-8 -*-
s = open(r"src/analyzers/timing_engine.py", encoding="utf-8").read()
old = "    return {\"cum_drop_5d\": round(cum_drop, 2), \"down_days_5d\": down_days}\n\n\nclass TimingEngine:"
new = "    return {\"cum_drop_5d\": round(cum_drop, 2), \"down_days_5d\": down_days}\n\n\ndef _volume_pattern_not_distribution(tech_data) -> bool:\n    '''量价分型是否不判出逃：放量下跌分歧型（外盘占优，承接/对倒不确定）\n    与资金接货型/抛压衰竭型属恐慌出清或分歧，不得按放量阴线-派发一刀切冻结。\n    修复模块间打架：量价判读免责与事件状态冻结互相矛盾。'''\n    try:\n        from .volume_pattern import build_volume_pattern\n        vp = build_volume_pattern(tech_data or {})\n        pattern = vp.get(\"pattern\") or \"\"\n        return pattern in (\"放量下跌分歧型\", \"资金接货型\", \"抛压衰竭型\")\n    except Exception:\n        return False\n\n\nclass TimingEngine:"
if old not in s:
    print("MISS anchor")
    raise SystemExit(1)
open(r"src/analyzers/timing_engine.py", "w", encoding="utf-8").write(s.replace(old, new, 1))
print("OK helper inserted")
