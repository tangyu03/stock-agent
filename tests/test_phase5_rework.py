"""
【Phase5 回炉】9/8 盘前报告验收 — 四个新问题的回归测试
=====================================================

验收实证（报告 2026-09-08 08:55，蘅东光/博杰/精智达三案例）：
  1. RRR 天文数字以"合法形态"回归——Z 线统一漏了缓冲层：
     精智达裸结构位 476.75 距买点 480.77 仅 0.84%，日振幅 8.16%，
     RRR=12.30 给置信度 +1（坏数据给好评）。
  2. 蘅东光创历史新高（盘中 555.10/收盘 549.50/前高 462.30），
     门一"创新高"未过——recent_high 含当日的口径 bug。
  3. 推导栏"禁用:确认追强"与闸门栏"追强降仓可用(三重门)"新旧并存；
     推导栏"置信度:高" vs 置信度栏"2/6 中"。
  4. 风险系数随机出现：精智达高波动拿 1.00，博杰反而 0.60。

修复锚点：buffered_structure 默认 + RRR 钳制 [2.5,5] + prior_high 口径
+ 推导栏同步 + 事件规则版本 + 波动率分档乘数。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.hypothesis import calculate_paired_stop, build_entry_hypothesis
from src.analyzers.signal_plan import (
    build_execution_plan,
    _volatility_tier_multiplier,
    compute_risk_budget_position,
)
from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore,
    SignalEvent,
    SignalLifecycle,
    reentry_status,
)
from src.analyzers.timing_engine import TimingEngine


# ============================================================
# 验收实证数据
# ============================================================

def _jingzhida_tech():
    """精智达 688627 9/7 收盘（报告 9/8 08:55 口径）：
    现价 491.00、MA25=476.75（结构位）、MA10=480.77（Y 主档）、
    W=[530.28, 564.65]、日振幅 8.16%（9/4 单日曾跌 8.13%）。"""
    return {
        "current_price": 491.00,
        "ma25": 476.75,
        "ma10": 480.77,
        "ma5": 474.33,
        "ma20": 465.0,
        "recent_high": 496.0,
        "tech_signals": {"vote_score": 2.0},
        "institutional_holding": {"vote_score": 0, "votes": {}},
        "market_score": 5.0,
        # 振幅序列：近端多日 >5%，当日 (555-510)/462 类比 8%+
        "kline": [
            {"收盘": 440, "最高": 448, "最低": 435},
            {"收盘": 441.58, "最高": 450, "最低": 438},
            {"收盘": 470, "最高": 482, "最低": 460},
            {"收盘": 491.00, "最高": 505.0, "最低": 466.5},
        ],
    }


def _hengdongguang_9_7_tech(**overrides):
    """蘅东光 920045 9/7：收盘 549.50（+18.86%）、盘中 555.10 创历史新高、
    9/4 涨停收盘 462.30（前期高点）、量比 1.54、量能 1.63x、ADX 48、
    外盘 +20.4%、RSI14=66.4。"""
    tech = {
        "current_price": 549.50,
        "recent_high": 555.10,     # 含当日的近 20 日高（历史新高）
        "prior_high": 462.30,      # 剔除当日的近 20 日高（9/4 涨停价）
        "ma25": 499.02,
        "ma10": 510.0,
        "ma20": 480.0,
        "ma5": 520.0,
        "volume_ratio": 1.54,
        "volume_vs_ma60": 1.63,
        "today_volume": 163000,            # 量能门：today/ma60 = 1.63x（报告实证）
        "volume_ma60": 100000,
        "adx": 48.0,
        "rsi": 66.4,               # RSI14
        "outer_volume": 144.92,
        "inner_volume": 95.84,
        "tech_signals": {"vote_score": 2.0},
        "institutional_holding": {"vote_score": 0, "votes": {}},
        "fundamental": {"profit_yoy": 113.3, "report_period": "2026-06"},   # 门二：净利+113.3%（报告实证）
        "market_score": 5.0,
    }
    tech.update(overrides)
    return tech


class TestJingzhidaZLineBuffer:
    """验收问题 1：RRR 12.30 的神话数字 → buffered 后回归诚实区间"""

    def test_buffered_stop_leaves_structure_with_buffer(self):
        """默认 buffered：结构位 476.75 下方留出 ATR 缓冲，不再裸用"""
        tech = _jingzhida_tech()
        z, ref, note = calculate_paired_stop(
            "价量突破", tech, benchmark_price=480.77, atr=23.3,
        )
        assert ref == pytest.approx(476.75, abs=0.01)   # 锚仍是结构位
        # ATR/ref=4.9% <5% → k=2.0 → buffer=46.6 被 8% 上限(38.14)钳制
        assert z == pytest.approx(438.61, abs=0.02)
        stop_distance = (480.77 - z) / 480.77
        assert stop_distance > 0.05                     # 不再是 0.84% 毫厘止损

    def test_rrr_back_to_honest_range(self):
        """RRR 从 12.30 回到 <2：裸位(480.77-476.75)=4.02 分母消失"""
        tech = _jingzhida_tech()
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=480.77,
            stop_loss=438.61,       # buffered Z
            target_range=[530.28, 564.65],
            tech_data=tech,
            sector_status="main_trend",
            hypothesis_x="突破MA25(昨收441.58在昨日MA25下方，今价491.00站上476.75)；量能突破60日均量(1.2倍)",
        )
        # (530.28-480.77)/(480.77-438.61) = 49.51/42.16 = 1.17
        assert plan.rrr_low == pytest.approx(1.17, abs=0.05)
        assert plan.rrr_low < 2.0                       # 诚实区间，不再是童话数字

    def test_rrr_quality_ticket_clamped(self):
        """RRR>5 不加分：12.30 的神话数字不构成质量证据（验收：坏数据不给好评）。
        场景复现：验收当日默认 bare 模式，0.84% 止损距离恰好过防噪下限(0.8%)，
        信号照常出厂、RRR 计算为 12.30——现在质量票被 cap 钳住不加分。"""
        tech = _jingzhida_tech()
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=480.77,
            stop_loss=476.75,       # 裸位（复现验收当日的 4.02 分母）
            target_range=[530.28, 564.65],
            tech_data=tech,
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
            gate_config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        # 0.84% > 裸位防噪下限 0.8% → 出厂不被拦（复现验收当日形态）
        assert plan.hypothesis_rejected is False
        assert plan.rrr_low == pytest.approx(12.30, abs=0.05)   # 口径复现
        # 12.3 > cap 5 → 不加分且显式说明
        assert not any("+1" in d and "RRR12.3" in d for d in plan.confidence_details)
        assert any("RRR12.3" in d and "不加分" in d for d in plan.confidence_details)
        # 默认 buffered 模式下同一止损距离直接被防噪拒绝（毫厘止损不出厂）
        plan_default = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=480.77,
            stop_loss=476.75,
            target_range=[530.28, 564.65],
            tech_data=tech,
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
        )
        assert plan_default.hypothesis_rejected is True

    def test_rrr_quality_ticket_in_range_still_counts(self):
        """正常区间 [2.5, 5] 照常加分（博杰 2.99 不受损）"""
        tech = _jingzhida_tech()
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=102.77,
            stop_loss=99.39,
            target_range=[112.89, 120.21],
            tech_data=tech,
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
        )
        assert any("RRR2.99" in d and "+1" in d for d in plan.confidence_details)

    def test_volatility_tier_multiplier_rules_risk(self):
        """验收问题 4：精智达振幅 8%+ → 风险乘数 0.6（不再随机拿 1.00）"""
        tech = _jingzhida_tech()
        # 当日 (505-466.5)/470 = 8.19% ≥ 8% → 0.6
        assert _volatility_tier_multiplier(tech) == 0.6
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=480.77,
            stop_loss=438.61,
            target_range=[530.28, 564.65],
            tech_data=tech,
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
        )
        assert plan.risk_multipliers.get("volatility_tier") == 0.6
        assert plan.combined_risk_multiplier == pytest.approx(0.6, abs=1e-9)

    def test_bojie_high_vol_not_dragged_to_limit_down(self):
        """博杰防宽：ATR/结构位 9.3% → 缓冲被 8% 上限压住，Z 不再=跌停价 85.54"""
        z, ref, _ = calculate_paired_stop(
            "价量突破", {"ma25": 99.39}, benchmark_price=102.77, atr=9.23,
        )
        assert z == pytest.approx(91.44, abs=0.02)
        assert z > 85.54 + 5        # 与制度边界拉开距离


class TestHengdongguangNewHigh:
    """验收问题 2：创历史新高的标的不被"创新高"条件误拦"""

    def test_gate1_new_high_uses_prior_high(self):
        """蘅东光 9/7：549.50 ≥ 前高 462.30×0.99 → 门一创新高通过"""
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(_hengdongguang_9_7_tech(), "main_trend")
        failed = [e for e in gate["evidence"] if "门一" in e]
        assert "创新高" not in "".join(failed)     # 唯一误拦项消失

    def test_gate1_new_high_full_pass(self):
        """三重门整体通过（量比/量能/ADX/外盘/RSI14 全过 + 主线）"""
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(_hengdongguang_9_7_tech(), "main_trend")
        assert gate["passed"] is True

    def test_old_bug_reproduced_without_prior_high(self):
        """旧口径复现：不传 prior_high（回退含当日 recent_high）→ 误拦"""
        tech = _hengdongguang_9_7_tech()
        del tech["prior_high"]      # 旧数据无该字段 → 回退 recent_high=555.10
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(tech, "main_trend")
        assert "创新高" in "".join(gate["evidence"])   # 549.50 < 555.10×0.99 差 0.05

    def test_no_new_high_still_blocked(self):
        """光力科技式回归（+7.77% 但未破前高）→ 创新高仍拦"""
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(
            _hengdongguang_9_7_tech(current_price=440.0),   # 远低于前高 462.30
            "main_trend",
        )
        assert "创新高" in "".join(gate["evidence"])

    def test_compute_prior_high_excludes_today(self):
        """prior_high 构建：近 N 日高剔除当日"""
        highs = [10, 11, 12, 13, 100]      # 当日 100 是新高
        assert TimingEngine._compute_prior_high(highs, 4) == 13
        assert TimingEngine._compute_prior_high([5], 4) is None

    def test_turtle_breakout_uses_prior_high(self):
        """海龟突破锚前期高点：蘅东光 549.50 vs 前高 462.30 → 突破成立"""
        from src.analyzers.timing_engine import StopLossCalc
        tech = _hengdongguang_9_7_tech()
        tech["ma20"] = 480.0
        # 构造 MA20 向上的 K 线（21 根，收盘递增）
        tech["kline"] = [
            {"收盘": 400 + i * 5, "最高": 405 + i * 5, "最低": 395 + i * 5,
             "开盘": 398 + i * 5}
            for i in range(21)
        ]
        te = TimingEngine(backtest_mode=True)
        stop = StopLossCalc(
            stock_code="920045", current_price=549.50,
            support_candidates=[], chosen_support=499.0,
            stop_loss_price=480.0, resistance=555.10,
        )
        sig = te._check_momentum_chase("920045", "蘅东光", tech, stop, "defend", "main_trend")
        # 防守模式：三重门全过 → 降仓放行信号诞生（不再是零提示）
        assert sig is not None
        assert sig.position_level == "probe"
        assert sig.defensive_chase is not None

    def test_rsi_gate_label_discloses_scope(self):
        """RSI 文案带口径（RSI14+阈值）——"同条件两判定"的观感质疑自消"""
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(
            _hengdongguang_9_7_tech(rsi=70.5), "main_trend",
        )
        assert any("RSI14未过热(<67)" in e for e in gate["evidence"])

    def test_reentry_new_high_uses_prior_high(self):
        """再入场待命：创新高锚 prior_high——蘅东光式头部待命不再漏"""
        store = InMemorySignalEventStore()
        event = SignalEvent(
            event_id="e1", stock_code="920045", stock_name="蘅东光",
            entry_type="价量突破", born_date="2026-09-04",
            expire_date="2026-09-11", stop_loss=438.0,
            status="invalidated", invalid_reason="止损",
        )
        store.save(event)
        status = reentry_status(
            "920045", current_price=549.50, recent_high=555.10,
            store=store, prior_high=462.30,
        )
        assert status["standby"] is True
        assert status["reason"] == "创新高"
        # 旧口径（不传 prior_high）差 0.05 元 → 待命不成立
        status_old = reentry_status(
            "920045", current_price=549.50, recent_high=555.10, store=store,
        )
        assert status_old["reason"] != "创新高"


class TestDerivationSync:
    """验收问题 3：推导栏与闸门栏/置信度栏同步"""

    def test_defend_derivation_no_longer_says_disabled(self):
        """防守模式推导栏：不再出现与闸门栏矛盾的"禁用:确认追强" """
        te = TimingEngine(backtest_mode=True)
        tech = _jingzhida_tech()
        derivation = te._build_derivation(
            tech, "defend", triggered_types=["价量突破"],
            selected_type="价量突破", confidence="中(2/6)",
            signal_direction="entry", sector_status="main_trend",
        )
        assert "禁用:确认追强" not in derivation
        assert "追强降仓需三重门" in derivation

    def test_merge_uses_execution_plan_confidence(self):
        """推导栏置信度优先取执行计划（含分数）——"高" vs "2/6 中"矛盾消除"""
        from src.analyzers.timing_engine import EntrySignal

        class _FakeSig(EntrySignal):
            pass

        sig = EntrySignal(
            stock_code="688627", stock_name="精智达", entry_type="价量突破",
            strategy_summary="s", entry_trigger_price=491.0, stop_loss=438.61,
            target_type="t", target_range=[530.28, 564.65],
            confidence="高",                       # EntrySignal 硬编码（旧口径）
        )
        sig.execution_plan = {
            "confidence": "中", "confidence_score": 2, "applicable_score": 6,
        }
        te = TimingEngine(backtest_mode=True)
        merged = te._merge_entry_signals([sig], _jingzhida_tech(), "defend", "main_trend")
        assert len(merged) == 1
        assert "置信度:中(2/6)" in merged[0].trigger_reason
        assert "置信度:高" not in merged[0].trigger_reason.split("④决策:")[1]


class TestEventRuleVersion:
    """验收问题 3b：存量事件与新规则双轨标注"""

    def _lifecycle(self):
        return SignalLifecycle(InMemorySignalEventStore(), valid_days=5)

    def test_register_event_records_rule_version(self):
        lc = self._lifecycle()
        event = lc.register_event(
            "002975", "博杰股份", "价量突破",
            breakout_level=99.39, entry_price=102.77, stop_loss=91.44,
            target_low=112.89, target_high=120.21,
            hypothesis={"x": "突破MA25", "y": 102.77, "z": 91.44, "w": [112.89, 120.21]},
            rule_version="buffered_structure",
        )
        assert event.rule_version == "buffered_structure"

    def test_status_note_marks_old_version(self):
        """存量事件（旧模式生成）在状态行标注旧版——审计者不再误判"""
        lc = self._lifecycle()
        lc.register_event(
            "002975", "博杰股份", "价量突破",
            breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
            target_low=112.89, target_high=120.21,
            hypothesis={}, rule_version="bare_structure",
        )
        note = lc.event_status_note(
            "002975", current_price=104.53,
            current_rule_version="buffered_structure",
        )
        assert "旧版" in note
        assert "结构位本体" in note
        assert "Z=85.54" in note

    def test_status_note_marks_legacy_without_version(self):
        """旧库事件（版本空）→ "旧版规则(版本未记录)" """
        store = InMemorySignalEventStore()
        store.save(SignalEvent(
            event_id="legacy", stock_code="002975", stock_name="博杰",
            entry_type="价量突破", born_date="2026-09-07",
            expire_date="2026-09-12", entry_price=102.77, stop_loss=85.54,
            status="valid",
        ))
        lc = SignalLifecycle(store, valid_days=5)
        note = lc.event_status_note("002975", current_rule_version="buffered_structure")
        assert "旧版规则(版本未记录)" in note

    def test_current_version_event_shows_clean_label(self):
        """当前版本事件：正常显示规则名，无"旧版"字样"""
        lc = self._lifecycle()
        lc.register_event(
            "688627", "精智达", "价量突破",
            breakout_level=476.75, entry_price=480.77, stop_loss=438.61,
            target_low=530.28, target_high=564.65,
            hypothesis={}, rule_version="buffered_structure",
        )
        note = lc.event_status_note(
            "688627", current_rule_version="buffered_structure",
        )
        assert "旧版" not in note
        assert "结构位加缓冲" in note

    def test_display_enums_are_chinese(self):
        from src.analyzers.signal_lifecycle import localize_display_enums

        localized = localize_display_enums("triggered buffered_structure bare_structure")
        assert localized == "已成交 结构位加缓冲 结构位本体"

    def test_independent_push_line_starts_with_stock_identity(self):
        from src.orchestrator.engine import _stock_push_line

        line = _stock_push_line({
            "stock_name": "博杰股份",
            "stock_code": "002975",
            "note": "triggered buffered_structure",
        })
        assert line.startswith("博杰股份(002975)")
        assert "triggered" not in line
        assert "buffered_structure" not in line
        assert "已成交" in line
        assert "结构位加缓冲" in line


class TestVolatilityTier:
    """验收问题 4：风险系数波动率分档（规则化，不随机）"""

    def test_high_amplitude_gets_0_6(self):
        # 当日 (high-low)/prev_close = (108-100)/100 = 8% ≥ 8% → 0.6
        kline = [
            {"收盘": 100, "最高": 101, "最低": 99},
            {"收盘": 100, "最高": 108, "最低": 100},
        ]
        assert _volatility_tier_multiplier({"kline": kline}) == 0.6

    def test_mid_amplitude_gets_0_8(self):
        kline = [
            {"收盘": 100, "最高": 101, "最低": 99},
            {"收盘": 100, "最高": 106, "最低": 100},   # 6%
        ]
        assert _volatility_tier_multiplier({"kline": kline}) == 0.8

    def test_low_amplitude_no_multiplier(self):
        kline = [
            {"收盘": 100, "最高": 101, "最低": 99},
            {"收盘": 100, "最高": 102.5, "最低": 99.5},  # 3%
        ]
        assert _volatility_tier_multiplier({"kline": kline}) is None

    def test_disabled_by_config(self):
        kline = [
            {"收盘": 100, "最高": 101, "最低": 99},
            {"收盘": 100, "最高": 108, "最低": 100},
        ]
        cfg = {"risk": {"volatility_tier": {"enabled": False}}}
        assert _volatility_tier_multiplier({"kline": kline}, cfg) is None

    def test_missing_kline_is_none(self):
        assert _volatility_tier_multiplier({}) is None

    def test_avg_amplitude_window_used(self):
        """近5日平均振幅口径：单日脉冲不永久降档，但平均高也降档"""
        kline = [
            {"收盘": 100, "最高": 101, "最低": 99},
            {"收盘": 100, "最高": 107, "最低": 100},   # 7%
            {"收盘": 100, "最高": 106, "最低": 100},   # 6%
        ]
        # 当日 6% ≥5% → 0.8（平均 6.5% 也 ≥5%）
        assert _volatility_tier_multiplier({"kline": kline}) == 0.8


class TestProbePositionWithBufferedStop:
    """连带校验：buffered 止损距离变大 → 试探仓按预算反推同步缩小"""

    def test_probe_ratio_budget_recalc(self):
        """精智达类：止损距离 8.8% → 仓位 = 1%÷8.8% ≈ 11%（封顶1/3以下）"""
        result = compute_risk_budget_position(480.77, 438.61)
        assert result["stop_distance_pct"] == pytest.approx(0.0878, abs=0.002)
        assert result["ratio"] == pytest.approx(0.01 / 0.0878, abs=0.005)
        assert result["capped"] is False

    def test_jingzhida_full_pipeline_probe(self):
        """蘅东光式三重门放行 → 试探仓用 buffered Z 反推（非 1/3 惯例）"""
        from src.analyzers.timing_engine import StopLossCalc
        tech = _hengdongguang_9_7_tech()
        tech["kline"] = [
            {"收盘": 400 + i * 5, "最高": 405 + i * 5, "最低": 395 + i * 5,
             "开盘": 398 + i * 5}
            for i in range(21)
        ]
        te = TimingEngine(backtest_mode=True)
        stop = StopLossCalc(
            stock_code="920045", current_price=549.50,
            support_candidates=[], chosen_support=499.0,
            stop_loss_price=480.0, resistance=555.10,
        )
        sig = te._check_momentum_chase("920045", "蘅东光", tech, stop, "defend", "main_trend")
        assert sig is not None and sig.position_level == "probe"
