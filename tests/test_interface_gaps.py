# -*- coding: utf-8 -*-
"""
层间接口缺失修复（2026-09-08 验收回炉）
=====================================
1. 策略层→分档层：追强类 Y 贴近触发位（不再一律 MA10 低吸档）
   ——蘅东光确认追强 Y=498.26 距现价 10.3%、罗博特科趋势延续 13.5%；
2. 评分层→决策层：低置信信号显著披露；
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
        reason = "①策略: 🟡防守 → 可用:恐慌抄底/套利低吸/价量突破/趋势延续\n  ②技术: 投票偏多↑(+2.0)\n  ③触发: 确认追强\n  ④决策: 选取确认追强 | 置信度:高"
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

    def test_stale_distribution_does_not_unretreat_structural_break(self, monkeypatch):
        """跌破MA5×0.99 是硬结构信号：派发日过期也不得把撤退降级成更不保守的防守。"""
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
        monkeypatch.setattr(mma, "_apply_external_shock", lambda mode: mode)
        # 先升后急跌：末5根跌破 MA5×0.99（结构撤退），且日期全在 2026-04（派发日过期）
        closes = [100.0 + i * 0.2 for i in range(35)]
        closes += [closes[-1] - (k + 1) * 1.8 for k in range(5)]
        kline = _kline_to(
            [f"2026-04-{(d % 28) + 1:02d}" for d in range(40)],
            closes=closes, volumes=[1000] * 40,
        )
        result = mma._assess_market("2026-09-08", kline)
        assert result["mode"] == "retreat"
        assert "已跌破MA5撤退为结构信号" in result["mode_reason"]

    def test_stale_distribution_forces_defend(self, monkeypatch):
        """过期派发日 → 模式降级 defend 且披露截至日（即使形态本应 attack）。"""
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
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
