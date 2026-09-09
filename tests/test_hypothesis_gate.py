"""
【一】可证伪假说出厂检查 — 回归测试

核心案例（沃尔德 2026-09-04）：
  买入 Y=93.88（MA10 回踩档），止损 Z=93.94（MA5×0.97 现价锚定）
  → Z >= Y 倒挂，原系统仅降置信度照发（RRR 0.00 落库为证），
    新系统在生成阶段直接拒绝。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.hypothesis import (
    TradeHypothesis,
    build_entry_hypothesis,
    calculate_paired_stop,
    validate_hypothesis,
    STRATEGY_EXIT_SPECS,
    calc_atr_from_kline,
)
from src.analyzers.signal_plan import build_execution_plan


def _kline(bars=80, base=90.0, vol=1_000_000, day_range=3.0):
    kline = []
    for i in range(bars):
        close = base + (i % 7) * 0.4
        kline.append({
            "date": f"2026-06-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + day_range / 2,
            "low": close - day_range / 2, "close": close,
            "volume": vol, "amount": vol * close,
        })
    return kline


def _tech_data(**overrides):
    data = {
        "current_price": 101.01,
        "ma5": 97.0, "ma10": 94.0, "ma20": 92.0, "ma25": 88.0,
        "ma25_prev": 88.5, "prev_close": 88.2, "today_open": 89.0,
        "volume_ratio": 2.0, "turnover_rate": 4.8,
        "kline": _kline(),
        "recent_high": 105.0,
    }
    data.update(overrides)
    return data


def plan_atr_reasons(plan):
    """拒绝理由统一取法：优先假说 dict，回退 rejection_reasons"""
    return plan.hypothesis.get("rejection_reasons", []) if plan.hypothesis else plan.rejection_reasons


class TestHypothesisValidation:
    """四要素完整性 + 不变式校验"""

    def test_missing_x_is_rejected(self):
        hyp = TradeHypothesis(strategy="价量突破", reason_x="",
                              entry_y=94.0, exit_z=83.5, exit_w=[109.0, 116.0])
        hyp = validate_hypothesis(hyp)
        assert not hyp.falsifiable
        assert any("买入理由缺失(X)" in r for r in hyp.rejection_reasons)

    def test_missing_z_is_rejected(self):
        hyp = TradeHypothesis(strategy="价量突破", reason_x="放量突破MA25",
                              entry_y=94.0, exit_z=0, exit_w=[109.0])
        hyp = validate_hypothesis(hyp)
        assert not hyp.falsifiable
        assert any("认错离场缺失(Z)" in r for r in hyp.rejection_reasons)

    def test_missing_w_is_rejected(self):
        hyp = TradeHypothesis(strategy="价量突破", reason_x="放量突破MA25",
                              entry_y=94.0, exit_z=83.5, exit_w=[])
        hyp = validate_hypothesis(hyp)
        assert not hyp.falsifiable
        assert any("兑现离场缺失(W)" in r for r in hyp.rejection_reasons)

    def test_wald_inverted_stop_is_rejected(self):
        """沃尔德 9/4 案例：止损 93.94 > 买点 93.88 → 假说自相矛盾，出厂拒绝"""
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=93.88,          # Y: MA10 回踩买点
            stop_loss=93.94,                # Z: 现价锚定 MA5×0.97（倒挂）
            target_range=[113.0],           # W
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25，回踩确认",
        )
        assert plan.hypothesis_rejected is True
        assert plan.execute is False
        assert any("止损倒挂" in r for r in plan.rejection_reasons)
        assert any("93.94" in r and "93.88" in r for r in plan.rejection_reasons)

    def test_too_narrow_stop_buffer_is_rejected(self):
        """【Phase5 回炉后语义】毫厘止损任何模式都拒绝：
        - 默认 buffered_structure：0.3% 宽度低于 min_z_buffer_pct(1%) → 拒绝
          （精智达 9/8 验收实证：0.84% 止损距离 vs 8.16% 日振幅，
          日内噪声十分之一即可扫损——沃尔德 0.3% 缓冲老病的新衣）；
        - bare 对照族：防噪下限 0.8% 以下同样拒绝；
        - atr_buffer 回退族：窄缓冲仍拒绝（旧行为）。"""
        # 默认 buffered_structure：0.3% 缓冲 → 拒绝
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=100.0,
            stop_loss=99.7,                 # 仅 0.3% 缓冲
            target_range=[108.0, 115.0],
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25",
        )
        assert plan.hypothesis_rejected is True
        assert any("止损缓冲不足" in r for r in plan_atr_reasons(plan))

        # 宽度足量（2%）：默认模式照常出厂
        plan_ok = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=100.0,
            stop_loss=98.0,                 # 2% 缓冲
            target_range=[108.0, 115.0],
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25",
        )
        assert plan_ok.hypothesis_rejected is False
        assert plan_ok.execute is True

        # 裸结构位对照族：0.3% < 防噪下限 0.8% → 同样拒绝
        plan_bare = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=100.0,
            stop_loss=99.7,
            target_range=[108.0, 115.0],
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25",
            gate_config={"hypothesis_gate": {"enabled": True, "z_line_mode": "bare_structure"}},
        )
        assert plan_bare.hypothesis_rejected is True
        assert any("日内噪声" in r for r in plan_atr_reasons(plan_bare))

        # 回退族 atr_buffer：窄缓冲仍拒绝（宽度由波动率决定）
        plan_atr = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=100.0,
            stop_loss=99.7,
            target_range=[108.0, 115.0],
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25",
            gate_config={"hypothesis_gate": {"enabled": True, "z_line_mode": "atr_buffer"}},
        )
        assert plan_atr.hypothesis_rejected is True
        assert any("止损缓冲不足" in r for r in plan_atr_reasons(plan_atr))

    def test_valid_hypothesis_passes_gate(self):
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=94.0,
            stop_loss=83.5,
            target_range=[121.0, 126.0],
            tech_data=_tech_data(),
            hypothesis_x="放量突破MA25，量能2.0倍",
        )
        assert plan.hypothesis_rejected is False
        assert plan.execute is True
        assert plan.hypothesis.get("sentence", "").startswith("因为")
        assert "说明我错了" in plan.hypothesis["sentence"]
        assert "兑现离场" in plan.hypothesis["sentence"]

    def test_gate_can_be_disabled_by_config(self):
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=93.88,
            stop_loss=93.94,
            target_range=[113.0],
            tech_data=_tech_data(),
            hypothesis_x="x",
            gate_config={"hypothesis_gate": {"enabled": False}},
        )
        assert plan.hypothesis_rejected is False
        assert plan.execute is True

    def test_atr_width_rule_uses_volatility(self):
        """【P0-2 + Phase5】Z 宽度规则：
        - atr_buffer 回退族：高 ATR 要求更宽止损（旧行为保留）；
        - buffered_structure（默认）：宽度下限 1.5×ATR，但被 8% 上限自洽压缩
          （防高波动标的被自己的 cap 与宽度检查夹击拒绝）；
        - bare 对照族：宽度由市场结构决定，但防噪下限仍在（0.3×ATR）。"""
        hyp = TradeHypothesis(strategy="价量突破", reason_x="放量突破",
                              entry_y=100.0, exit_z=98.0, exit_w=[108.0])
        # atr_buffer 模式：ATR=4.0 → 最小宽度 6.0，当前宽度 2.0 → 拒绝
        hyp = validate_hypothesis(
            hyp, atr=4.0,
            config={"hypothesis_gate": {"enabled": True, "z_line_mode": "atr_buffer"}},
        )
        assert not hyp.falsifiable
        assert any("止损缓冲不足" in r for r in hyp.rejection_reasons)
        # atr_buffer 模式：ATR=1.0 → 最小宽度 1.5，当前宽度 2.0 → 通过
        hyp2 = TradeHypothesis(strategy="价量突破", reason_x="放量突破",
                               entry_y=100.0, exit_z=98.0, exit_w=[108.0])
        hyp2 = validate_hypothesis(
            hyp2, atr=1.0,
            config={"hypothesis_gate": {"enabled": True, "z_line_mode": "atr_buffer"}},
        )
        assert hyp2.falsifiable
        # 默认 buffered 模式：ATR=4 → min(1.5×4, 8%×100) = 6 → 宽度 2.0 → 拒绝
        hyp3 = TradeHypothesis(strategy="价量突破", reason_x="放量突破",
                               entry_y=100.0, exit_z=98.0, exit_w=[108.0])
        hyp3 = validate_hypothesis(hyp3, atr=4.0)
        assert not hyp3.falsifiable
        # 默认 buffered 模式 + 高波动：ATR=30 → 1.5×30=45 被 8% 上限压到 8
        # → 宽度 2.0 仍拒绝，但拒绝理由是 8% 上限而非 45（自洽不矛盾）
        hyp4 = TradeHypothesis(strategy="价量突破", reason_x="放量突破",
                               entry_y=100.0, exit_z=92.0, exit_w=[108.0])
        hyp4 = validate_hypothesis(hyp4, atr=30.0)
        assert hyp4.falsifiable
        # 裸结构位对照族：同一窄止损不受 ATR 预算约束（博杰 9/7 重算实证），
        # 但防噪下限（max(0.8%, 0.3×ATR)=1.2）仍拦更低者
        hyp5 = TradeHypothesis(strategy="价量突破", reason_x="放量突破",
                               entry_y=100.0, exit_z=98.0, exit_w=[108.0])
        hyp5 = validate_hypothesis(
            hyp5, atr=4.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert hyp5.falsifiable


class TestPairedStopCalculation:
    """【四/P0-2 + Phase5】Z = X 的直接否定（默认 buffered_structure；
    bare/atr_buffer 为对照族与回退族）"""

    def test_breakout_stop_anchors_to_breakout_level(self):
        """价量突破 Z 锚定突破位（MA25）：默认 buffered = 结构位−clamp(k×ATR)，
        裸结构位对照族 Z=结构位本体"""
        z, ref, note = calculate_paired_stop(
            "价量突破", _tech_data(), benchmark_price=94.0, atr=3.0,
        )
        assert ref == 88.0                      # 突破位 = MA25
        # ATR/ref = 3/88 = 3.4% < 5% → 低波动档 k=2.0 → buffer=6.0（<8%上限7.04）
        assert z == pytest.approx(82.0, abs=0.01)  # 默认 buffered
        assert "跌回突破位" in note
        # 裸结构位对照族：Z = 结构位本体
        z_bare, _, _ = calculate_paired_stop(
            "价量突破", _tech_data(), benchmark_price=94.0, atr=3.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert z_bare == 88.0
        # 回退族：结构位 - 1.5×ATR
        z_atr, _, _ = calculate_paired_stop(
            "价量突破", _tech_data(), benchmark_price=94.0, atr=3.0,
            config={"hypothesis_gate": {"z_line_mode": "atr_buffer"}},
        )
        assert z_atr < 88.0 * (1 - 0.005)
        assert z_atr <= 88.0 - 1.5 * 3.0 + 1e-9

    def test_momentum_stop_anchors_to_ma20(self):
        z, ref, _ = calculate_paired_stop(
            "确认追强", _tech_data(ma20=92.0), benchmark_price=101.0, atr=2.0,
        )
        assert ref == 92.0
        # ATR/ref = 2/92 = 2.2% → k=2.0 → buffer=4.0（<8%上限7.36）
        assert z == pytest.approx(88.0, abs=0.01)  # 默认 buffered
        z_bare, _, _ = calculate_paired_stop(
            "确认追强", _tech_data(ma20=92.0), benchmark_price=101.0, atr=2.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert z_bare == 92.0                   # 裸结构位对照

    def test_panic_stop_anchors_to_panic_low(self):
        """恐慌抄底 Z 锚定恐慌低点（反弹失败再创新低即认错）"""
        tech = _tech_data()
        tech["kline"] = _kline(bars=80, base=95.0)
        z, ref, note = calculate_paired_stop(
            "恐慌抄底", tech, benchmark_price=95.0, atr=3.0,
        )
        assert z < ref                           # 默认 buffered：低点下方留缓冲
        assert "新低" in note
        z_bare, ref_bare, _ = calculate_paired_stop(
            "恐慌抄底", tech, benchmark_price=95.0, atr=3.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert z_bare == ref_bare                # 裸结构位对照（恐慌低点本体）

    def test_arbitrage_stop_anchors_to_structure(self):
        z, ref, note = calculate_paired_stop(
            "套利低吸", _tech_data(), benchmark_price=94.0, atr=2.0,
        )
        # 结构位 = min(近5日低, MA10)；低吸结构破位即认错
        assert z < ref                           # 默认 buffered：结构位下方留缓冲
        assert "破位" in note
        z_bare, ref_bare, _ = calculate_paired_stop(
            "套利低吸", _tech_data(), benchmark_price=94.0, atr=2.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert z_bare == ref_bare                # 裸结构位对照


class TestStrategyExitSpecs:
    """四策略必须自带配对出场（定义买入的那一刻就定义卖出）"""

    def test_all_four_strategies_have_z_and_w(self):
        for strategy in ("价量突破", "恐慌抄底", "套利低吸", "确认追强"):
            spec = STRATEGY_EXIT_SPECS[strategy]
            assert spec["z_rule"], f"{strategy} 缺认错离场 Z"
            assert spec["w_rule"], f"{strategy} 缺兑现离场 W"
            assert spec["z_reference"], f"{strategy} 缺 Z 锚定结构位"

    def test_hypothesis_sentence_contains_all_four_elements(self):
        hyp = build_entry_hypothesis(
            entry_type="价量突破",
            tech_data=_tech_data(),
            benchmark_price=94.0,
            target_range=[109.0, 116.0],
            trigger_reason="放量突破MA25，量能2.0倍",
            atr=3.0,
        )
        assert hyp.falsifiable
        sentence = hyp.sentence()
        assert "因为放量突破MA25" in sentence
        assert "在94.00买入" in sentence
        assert "说明我错了" in sentence
        assert "兑现离场" in sentence


class TestAtrHelper:
    def test_calc_atr_from_kline(self):
        atr = calc_atr_from_kline(_kline(day_range=3.0), period=14)
        assert atr is not None and 2.5 < atr < 3.5

    def test_calc_atr_insufficient_data(self):
        assert calc_atr_from_kline(_kline(bars=10)) is None
