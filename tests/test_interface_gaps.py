# -*- coding: utf-8 -*-
"""
层间接口缺失修复（2026-09-08 验收回炉）
=====================================
1. 策略层→分档层：追强类 Y 贴近触发位（不再一律 MA10 低吸档）
   ——蘅东光确认追强 Y=498.26 距现价 10.3%、罗博特科趋势延续 13.5%；
2. 评分层→决策层：0/6 低置信/负期望不再放行（期望值闸门连闸）；
3. 仓位层→账户层：预算绝对值显式披露（蘅东光 26,568 敞口→隐含
   265 万账户，不再黑箱）；
4. 推导栏置信度：与置信度栏同源（高/中 vs 0/6 低矛盾进第五轮）；
5. 派发日新鲜度：数据过期时模式降级防守并披露截至日。
"""
import pytest

from src.analyzers.signal_plan import (
    build_execution_plan,
    build_volume_snapshot,
    build_fund_snapshot,
    _build_execution_tiers,
)
from src.analyzers.timing_engine import (
    entry_buypoint,
    entry_main_tier_price,
    TimingEngine,
)
from src.decision.live_scheduler import (
    schedule_live_signals,
    _budget_disclosure_note,
)
from src.loop.market_mode_adaptive import MarketModeAdaptive


# ---------------------------------------------------------------
# 1. 策略层 → 分档层：Y 定位随策略分化
# ---------------------------------------------------------------

class TestStrategyAwareY:
    def test_strategy_specific_y(self):
        """追强类 Y 不再用 MA10 低吸模板。"""
        assert entry_buypoint("确认追强", 575.00, {}, {}) == (
            pytest.approx(563.50, abs=0.01), "trigger_price*(1-chase_pullback_pct)",
            {"trigger_price": 575.0, "chase_pullback_pct": 0.02},
        )
        assert entry_buypoint("价量突破", 112.89, {"breakout_low": 102.07}, {}) == (
            102.07, "breakout_day_low", {"breakout_day_low": 102.07},
        )
        assert entry_buypoint(
            "趋势延续", 656.10, {"ma5": 628.00, "prev_day_low": 615.00}, {},
        ) == (
            628.00, "max(ma5,prev_day_low)",
            {"ma5": 628.00, "prev_day_low": 615.00},
        )
        assert entry_buypoint(
            "趋势延续", 656.10, {"ma5": 605.00, "prev_day_low": 615.00}, {},
        ) == (
            615.00, "max(ma5,prev_day_low)",
            {"ma5": 605.00, "prev_day_low": 615.00},
        )
        assert entry_main_tier_price(
            "确认追强", 555.25, {"ma10": 498.26}, {},
        ) == pytest.approx(555.25 * 0.98, abs=0.01)
        assert entry_main_tier_price(
            "价量突破", 555.25, {"breakout_low": 555.25}, {},
        ) == 555.25
        assert entry_main_tier_price(
            "趋势延续", 656.10, {"ma5": 628.00}, {},
        ) == 628.00

    def test_dip_strategies_keep_ma10_y(self):
        """低吸类保留 MA10 回踩承接语义。"""
        assert entry_main_tier_price("套利低吸", 555.25, {"ma10": 498.26}) == 498.26
        assert entry_main_tier_price("恐慌抄底", 555.25, {"ma10": 498.26}) == 498.26

    def test_chase_tiers_anchor_trigger_not_ma10(self):
        """追强分档：主档=触发位下方2%，试探=再浅回踩。"""
        volume = build_volume_snapshot(
            {"kline": [{"volume": 1000, "turnover_rate": 1.0} for _ in range(61)]}
        )
        fund = build_fund_snapshot(None, {})
        tech = {"current_price": 555.25, "ma5": 520.0, "ma10": 498.26}
        tiers = _build_execution_tiers(
            544.14, 466.69, tech, volume, fund, "确认追强", {},
        )
        assert tiers[0]["name"] == "追强档"
        assert tiers[0]["price"] == pytest.approx(544.14, abs=0.01)
        assert tiers[1]["name"] == "浅回踩档"
        assert tiers[1]["price"] == pytest.approx(544.14 * 0.98, abs=0.01)

    def test_dip_tiers_keep_ma_template(self):
        """低吸分档：MA10 主档 + MA5 试探档（原模板）。"""
        volume = build_volume_snapshot(
            {"kline": [{"volume": 1000, "turnover_rate": 1.0} for _ in range(61)]}
        )
        fund = build_fund_snapshot(None, {})
        tech = {"current_price": 555.25, "ma5": 520.0, "ma10": 498.26}
        tiers = _build_execution_tiers(
            498.26, 466.69, tech, volume, fund, "套利低吸", {},
        )
        assert tiers[0]["name"] == "MA10档"
        assert tiers[1]["name"] == "MA5档"


# ---------------------------------------------------------------
# 2. 评分层 → 决策层：期望值闸门连闸
# ---------------------------------------------------------------

def _zero_score_tech():
    """0/6 场景：仅剩“矛盾0”可读，无任何加分项。"""
    return {
        "current_price": 101.61,
        "ma5": 99.0, "ma10": 98.0, "ma20": 97.0,
        "tech_signals": {
            "vote_score": 0, "category_votes": {},
            "chan_divergence": {"type": "顶背驰", "confidence": "中"},
        },
    }


class TestExpectedValueGate:
    def test_zero_six_confidence_no_longer_passes(self):
        """0/6 低置信（RRR0.96 类）不再放行：execute=False。"""
        plan = build_execution_plan(
            "价量突破", 498.26, 409.70, [583.01],
            tech_data=_zero_score_tech(),
        )
        assert plan.confidence_score == 0
        assert plan.execute is False
        assert plan.ev_gate.get("rejected") is True
        assert any("期望值闸门" in r for r in plan.rejection_reasons)
        # EV = W×R−(1−W)：RRR0.96 隐含盈亏平衡胜率 ~51%，0/6 拿不出证据
        assert "盈亏平衡" in " ".join(plan.rejection_reasons)

    def test_scored_signal_still_executes(self):
        """评分≥1 且有质量证据的信号照常放行。"""
        tech = _zero_score_tech()
        tech["market_score"] = 6.0
        tech["tech_signals"] = {
            "category_votes": {
                "trend": {"vote": 1}, "momentum": {"vote": 1},
                "pattern": {"vote": 1}, "volume": {"vote": 1},
            }
        }
        plan = build_execution_plan(
            "价量突破", 10.0, 9.5, [11.5],
            tech_data=tech, sector_status="main_trend",
            hypothesis_x="放量突破MA25",
        )
        assert plan.confidence_score >= 1
        assert plan.execute is True
        assert plan.ev_gate.get("rejected") is False

    def test_ev_gate_uses_real_strategy_win_rate(self, monkeypatch):
        """策略统计足样本时用真实胜率算 EV；负期望拒绝。"""
        stats = {
            "价量突破": {"trades": 40, "win_rate": 0.35},
        }
        monkeypatch.setattr(
            "src.feedback.strategy_stats.compute_strategy_stats",
            lambda: stats,
        )
        # R=1.0：EV = 0.35×1.0−(1−0.35) = −0.30 < 0 → 拒绝
        plan = build_execution_plan(
            "价量突破", 10.0, 9.0, [11.0],
            tech_data={"market_score": 6.0,
                       "tech_signals": {"category_votes": {
                           "trend": {"vote": 1}, "momentum": {"vote": 1},
                           "pattern": {"vote": 1}, "volume": {"vote": 1}}}},
            sector_status="main_trend",
            hypothesis_x="放量突破MA25",
        )
        assert plan.ev_gate.get("rejected") is True
        assert "策略实测40笔" in plan.ev_gate.get("win_rate_source", "")

    def test_ev_gate_can_be_disabled(self):
        """ev_gate.enabled=false 回退旧行为（0/6 只降档不拒绝）。"""
        plan = build_execution_plan(
            "价量突破", 10.0, 9.0, [11.0],
            tech_data=_zero_score_tech(),
            gate_config={"hypothesis_gate": {"ev_gate": {"enabled": False}}},
        )
        assert plan.ev_gate.get("rejected") is False

    def test_scheduler_dedicated_ev_gate_bucket(self):
        """调度器把 EV 拒绝单独立桶（buy_ev_gate），不进假说拒桶。"""
        plan = build_execution_plan(
            "价量突破", 498.26, 409.70, [583.01],
            tech_data=_zero_score_tech(),
        )
        sig = {
            "stock_code": "EV01", "stock_name": "EV拒",
            "entry_type": "价量突破", "trigger_price": 498.26,
            "confidence": "低", "execution_plan": plan.as_dict(),
        }
        scheduled = schedule_live_signals([sig], [], market_mode="defend")
        assert scheduled["stats"]["buy_ev_gate"] == 1
        assert scheduled["stats"]["buy_hypothesis_rejected"] == 0
        assert len(scheduled["buy"]) == 0
        assert scheduled["skipped"]["buy_ev_gate"][0]["stock_code"] == "EV01"


# ---------------------------------------------------------------
# 3. 仓位层 → 账户层：预算绝对值显式披露
# ---------------------------------------------------------------

class TestBudgetDisclosure:
    def test_hengdongguang_exposure_and_implied_account(self):
        """蘅东光 9/8 场景：300股×88.56=26,568 敞口→隐含账户披露。"""
        note = _budget_disclosure_note(300, 555.25, 466.69, 0.01)
        assert "单笔风险敞口26,568元" in note
        assert "300股×88.56" in note
        assert "反推隐含账户266万" in note
        assert "账户100万" in note

    def test_scheduler_note_discloses_budget(self):
        """调度 note 携带预算口径（不再黑箱）。"""
        plan = build_execution_plan(
            "价量突破", 10.0, 9.0, [11.5],
            tech_data={"market_score": 6.0,
                       "tech_signals": {"category_votes": {
                           "trend": {"vote": 1}, "momentum": {"vote": 1},
                           "pattern": {"vote": 1}, "volume": {"vote": 1}}}},
            sector_status="main_trend",
            hypothesis_x="放量突破MA25",
        )
        sig = {
            "stock_code": "BUD01", "stock_name": "预算披露",
            "entry_type": "价量突破", "trigger_price": 10.0,
            "confidence": "高", "benchmark_price": 10.0, "rrr_low": 2.5,
            "hypothesis": {"z": 9.0},
            "execution_plan": plan.as_dict(),
        }
        scheduled = schedule_live_signals([sig], [], market_mode="attack")
        note = scheduled["buy"][0].schedule_note
        assert "单笔风险敞口" in note
        assert "预算基数25万" in note

    def test_buy_push_renders_budget_disclosure(self):
        """买入推送透出预算披露（此前被 ⑤触发 按" | 调度: "切掉）。"""
        from src.push.templates import _render_compact_entry_signal
        plan = build_execution_plan(
            "价量突破", 10.0, 9.0, [11.5],
            tech_data={"market_score": 6.0,
                       "tech_signals": {"category_votes": {
                           "trend": {"vote": 1}, "momentum": {"vote": 1},
                           "pattern": {"vote": 1}, "volume": {"vote": 1}}}},
            sector_status="main_trend",
            hypothesis_x="放量突破MA25",
        )
        disclosure = _budget_disclosure_note(100, 10.0, 9.0, 0.01)
        title, content = _render_compact_entry_signal({
            "stock_code": "BUD01", "stock_name": "预算披露",
            "entry_type": "价量突破", "execution_plan": plan.as_dict(),
            "note": f"突破确认 | 调度: 基准10.00 | 建议100股 | {disclosure}",
        })
        assert "预算披露" in content
        assert "预算基数25万" in content
        assert "单笔风险敞口" in content
        assert "反推隐含账户" in content


# ---------------------------------------------------------------
# 4. 推导栏置信度与置信度栏同源
# ---------------------------------------------------------------

class TestDerivationConfidenceSync:
    def test_refresh_derivation_confidence(self):
        """推导栏④行从 EntrySignal 硬编码“高”刷新为评分体系“低(0/6)”。"""
        engine = TimingEngine.__new__(TimingEngine)
        reason = "确认追强: 海龟突破\n  推导: ④决策: 选取确认追强 | 置信度:高"
        plan = type("Plan", (), {
            "confidence_score": 0, "applicable_score": 6, "confidence": "低",
        })()
        refreshed = engine._refresh_derivation_confidence(reason, plan)
        assert "置信度:低(0/6)" in refreshed
        assert "置信度:高" not in refreshed

    def test_refresh_preserves_derivation_lines(self):
        """刷新只动置信度，不改推导链其他行。"""
        engine = TimingEngine.__new__(TimingEngine)
        reason = "①策略: 🟡防守 → 可用:恐慌抄底/套利低吸/价量突破/趋势延续 | 追强降仓需三重门\n  ②技术: 投票偏多↑(+2.0)\n  ③触发: 确认追强\n  ④决策: 选取确认追强 | 置信度:高"
        plan = type("Plan", (), {
            "confidence_score": 2, "applicable_score": 6, "confidence": "中",
        })()
        refreshed = engine._refresh_derivation_confidence(reason, plan)
        assert "①策略:" in refreshed
        assert "②技术:" in refreshed
        assert "③触发:" in refreshed
        assert "置信度:中(2/6)" in refreshed


# ---------------------------------------------------------------
# 5. 派发日新鲜度：过期数据降级 + 披露
# ---------------------------------------------------------------

def _kline_to(dates, closes=None, volumes=None):
    n = len(dates)
    return [
        {"date": dates[i], "close": closes[i] if closes else 100.0 + i,
         "volume": volumes[i] if volumes else 1000}
        for i in range(n)
    ]


class TestDistributionDayFreshness:
    def test_count_distribution_marks_stale(self):
        """末根距参考日超 7 天 → stale=True + last_date 披露。"""
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
        kline = _kline_to([f"2026-04-{d:02d}" for d in range(1, 26)])
        dist = mma._count_distribution_days(kline, ref_date="2026-09-08")
        assert dist["stale"] is True
        assert dist["last_date"] == "2026-04-25"
        assert dist["stale_days"] >= 100

    def test_fresh_data_not_stale(self):
        """末根即参考日 → 不标记过期。"""
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
        kline = _kline_to([f"2026-09-{d:02d}" for d in range(1, 26)])
        dist = mma._count_distribution_days(kline, ref_date="2026-09-08")
        assert dist["stale"] is False

    def test_stale_distribution_forces_defend(self, monkeypatch):
        """过期派发日 → 模式降级 defend 且披露截至日（即使形态本应 attack）。"""
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
        # 隔离外盘扰动与 S3 降级：仅验证“过期派发日→defend”这条链路
        # （实时盘中缺外盘数据时会被当作严重扰动再降级，与本用例无关）
        monkeypatch.setattr(mma, "_apply_external_shock", lambda mode: mode)
        from src.analyzers.expectation_divergence import DivergenceCounter
        monkeypatch.setattr(
            DivergenceCounter, "load",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("test: skip S3"))),
        )
        # 40 根上升 K 线（多头排列+站上5日线 → 本应 attack），
        # 全部收在 2026-04 且等量（无派发日）——但相对 9/8 已严重过期
        kline = _kline_to(
            [f"2026-04-{(d % 28) + 1:02d}" for d in range(40)],
            closes=[100.0 + i * 0.2 for i in range(40)],
            volumes=[1000] * 40,
        )
        result = mma._assess_market("2026-09-08", kline)
        assert result["mode"] == "defend"
        assert "派发日数据过期" in result["mode_reason"]
        assert "2026-04" in result["mode_reason"]
