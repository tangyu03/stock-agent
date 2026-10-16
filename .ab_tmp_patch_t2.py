# -*- coding: utf-8 -*-

def patch(path, pairs, label):
    s = open(path, encoding="utf-8").read()
    for old, new in pairs:
        if old not in s:
            print("[MISS] " + label + ": " + old[:60])
            raise SystemExit(1)
    for old, new in pairs:
        s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("[OK] " + label)

# ---- test_hypothesis_gate.py：负RRR 目标重锚 ----
patch(r"tests/test_hypothesis_gate.py", [
    (
"""    def test_valid_hypothesis_passes_gate(self):""",
"""    def test_negative_rrr_target_reanchored_above_buy_point(self):
        \"\"\"负盈亏比硬伤：目标W低于买点Y（参考系错位）→ 出厂前上抬到Y上方，RRR>0。\"\"\"
        hyp = build_entry_hypothesis(
            entry_type="恐慌抄底",
            tech_data=_tech_data(current_price=170.0),
            benchmark_price=205.92,           # 买点=高位回踩位（远高于现价）
            target_range=[200.88, 206.0],     # 目标=现价测算位（参考系错位 → 原会负RRR）
            trigger_reason="上证暴跌+个股超卖",
        )
        assert hyp.entry_y == 205.92
        assert max(hyp.exit_w) > hyp.entry_y * 1.005
        assert hyp.exit_z < hyp.entry_y
        rrr = (max(hyp.exit_w) - hyp.entry_y) / (hyp.entry_y - hyp.exit_z)
        assert rrr > 0
        assert hyp.falsifiable is True

    def test_positive_rrr_untouched_by_reanchor(self):
        \"\"\"正常正RRR（目标已在买点上方）→ 重锚不生效，保持原目标。\"\"\"
        hyp = build_entry_hypothesis(
            entry_type="恐慌抄底",
            tech_data=_tech_data(current_price=101.01),
            benchmark_price=101.01,
            target_range=[109.0, 119.0],
            trigger_reason="上证暴跌+个股超卖",
        )
        assert hyp.exit_w == [109.0, 119.0]
        assert max(hyp.exit_w) > hyp.entry_y

    def test_valid_hypothesis_passes_gate(self):""",
    ),
], "hypothesis RRR test")

# ---- test_timing_engine.py：量价不判出逃 ----
patch(r"tests/test_timing_engine.py", [
    (
"""class TestPanicBottomCumulativeAndGate:""",
"""    def test_volume_pattern_not_distribution(self):
        \"\"\"量价判读不判出逃（放量下跌分歧型）→ 冻结豁免依据为真。\"\"\"
        import src.analyzers.timing_engine as te_mod
        from src.analyzers.volume_pattern import _DIVERGENCE_PATTERN
        assert _DIVERGENCE_PATTERN == "放量下跌分歧型"
        tech = {
            "current_price": 100.0, "change_pct": -5.0,
            "volume_ratio": 2.5, "turnover_rate": 5.0,
            "tech_signals": {
                "order_flow": {"outer_volume": 60, "inner_volume": 40, "available": True},
                "volume_snapshot": {"volume_ratio": 2.5, "turnover_rate": 5.0},
            },
            "ma5": 105.0, "ma10": 108.0, "ma20": 110.0,
            "recent_high": 120.0, "prior_high": 120.0, "gain_20d": 0.1, "ma20_falling": True,
        }
        assert te_mod._volume_pattern_not_distribution(tech) is True
        # 真实抛压（主动差≤-8%）→ 不豁免（该冻结）
        tech["tech_signals"]["order_flow"] = {
            "outer_volume": 30, "inner_volume": 70, "available": True}
        assert te_mod._volume_pattern_not_distribution(tech) is False


class TestPanicBottomCumulativeAndGate:""",
    ),
], "timing_engine pattern test")

print("TESTS ADDED")
