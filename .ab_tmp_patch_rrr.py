# -*- coding: utf-8 -*-

def patch(path, pairs, label):
    s = open(path, encoding="utf-8").read()
    for old, new in pairs:
        if old not in s:
            print(f"[MISS] {label}: {old[:70]!r}")
            raise SystemExit(1)
    for old, new in pairs:
        s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print(f"[OK] {label}")

# ---- 1. hypothesis.py：目标W锚定买点Y之上（修复负RRR参考系错位） ----
patch(r"src/analyzers/hypothesis.py", [
    (
"""    else:
        y_note = "主档回踩承接"

    hyp = TradeHypothesis(""",
"""    else:
        y_note = "主档回踩承接"

    # 【负RRR硬伤修复】目标W与买点Y必须同一参考系：target_range 按"现价"测算
    # （恐慌抄底=现价×1.08~1.18），而买点Y=entry_buypoint(高位回踩位)，参考系错位
    # → 出现"目标低于买点、负盈亏比"的自相矛盾参数（胜宏205.92买/200.88目标）。
    # 出厂前把 W 上抬到 Y×(1+min_w_profit_pct) 之上，保证 RRR>0；正常情况不动。
    min_profit_pct = float(
        _config_get(config, "hypothesis_gate", "min_w_profit_pct", default=0.05)
    )
    if benchmark > 0 and targets and max(targets) <= benchmark * 1.005:
        floor_w = round(benchmark * (1 + min_profit_pct), 2)
        targets = [max(t, floor_w) for t in targets]
        w_note = (
            f"{spec.get('w_rule', '到达目标位兑现')}"
            f"(目标上抬至买点上方{min_profit_pct * 100:.0f}%,修复负盈亏比)"
        )

    hyp = TradeHypothesis(""",
    ),
], "hypothesis W re-anchor")

# ---- 2. timing_engine.py：冻结决策纳入量价分型"不判出逃"（放量下跌分歧型） ----
patch(r"src/analyzers/timing_engine.py", [
    (
"""                change_pct=tech_data.get("change_pct"),
                capitulation_evidence=bool(
                    tech_data.get("long_lower_shadow")
                    or tech_data.get("shrinking_stabilize")
                    or tech_data.get("has_hammer")
                ),
            )""",
"""                change_pct=tech_data.get("change_pct"),
                capitulation_evidence=bool(
                    tech_data.get("long_lower_shadow")
                    or tech_data.get("shrinking_stabilize")
                    or tech_data.get("has_hammer")
                    or _volume_pattern_not_distribution(tech_data)
                ),
            )""",
    ),
], "timing_engine freeze pattern")

print("DONE")
