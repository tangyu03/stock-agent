# -*- coding: utf-8 -*-
p = r"src/feedback/event_tracker.py"
s = open(p, encoding="utf-8").read()
old = """    covered_keys = {(r["stock_code"], r.get("entry_type") or r["entry_type"]) for r in rows}
    for e in normalized:
        born_day = _to_date(e.get("born_date"))
        if born_day is None or not (window_start <= born_day <= as_of):
            continue
        code = e.get("stock_code", "")
        etype = e.get("entry_type", "")
        if (code, etype) in covered_keys:
            continue  # 闭合交易已覆盖同一事件"""
new = """    covered_keys = {(r["stock_code"], r.get("entry_type") or r["entry_type"]) for r in rows}
    # 【重复条目/计数矛盾修复】同一 event_id 只显示一次（参数完全相同的一对
    # 行必是同一事件，不去重会导致“真实撤单数”与“结构失败撤单N”对不上）。
    seen_event_ids: set = set()
    for e in normalized:
        eid = str(e.get("event_id") or "")
        if eid:
            if eid in seen_event_ids:
                continue
            seen_event_ids.add(eid)
        born_day = _to_date(e.get("born_date"))
        if born_day is None or not (window_start <= born_day <= as_of):
            continue
        code = e.get("stock_code", "")
        etype = e.get("entry_type", "")
        if (code, etype) in covered_keys:
            continue  # 闭合交易已覆盖同一事件"""
assert old in s
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("OK dedup")
