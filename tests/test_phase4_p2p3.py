"""
【Phase4 P2/P3】板块版本化 / 资金拆层 / 组合预算 / 趋势延续 / 驱动源 / 参数附录
— 回归测试（每项带一句话依据与作废条件锚定）

| 任务 | 依据 | 作废条件 |
| P2 板块版本化 | 9/4→9/7 分类整体换血，历史统计断裂 | 新分类经一季度验证区分度优于申万锚 |
| P2 资金拆层 | 股东户数6/30、主力3日快照对当日价格投票 | 慢变量对5日收益有显著预测力 |
| P2 组合预算 | 9/7 观察区23/24主线、半导体设备8只同标签 | 集中持有主线期望值更高则放宽上限 |
| P3 趋势延续 | 沃尔德 9/7 +7.02% 处于突破后延续段，四策略无一覆盖 | 延续段期望值低于突破日入场 |
| P3 第八问驱动源 | 博杰 9/7 真实驱动是业绩+PCB联动，系统只看到价格表象 | 驱动源与5日收益无显著相关 |
| P3 参数附录 | RRR 1.5 隐含40%胜率假设七天未验证 | 参数被回测推翻时更新，附录机制永不作废 |
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


import src.analyzers.institutional_scorer as _inst_module
_REAL_SCORE_INSTITUTIONAL = _inst_module.score_institutional_holding


@pytest.fixture(autouse=True)
def _no_institutional(monkeypatch):
    import os
    os.environ.setdefault("TQDM_DISABLE", "1")
    import src.analyzers.institutional_scorer as _inst
    monkeypatch.setattr(
        _inst, "score_institutional_holding",
        lambda c, *a, **kw: {"vote_score": 0, "vote_label": "skip",
                             "votes": {}, "bullish_count": 0,
                             "bearish_count": 0, "neutral_count": 4, "stale": False},
    )


# ============================================================
# P2-1 板块版本化
# ============================================================

class TestSectorVersioning:
    def test_theme_version_stamp_exists(self):
        from src.analyzers.theme_attribution import get_theme_version
        version = get_theme_version()
        assert version and version != "v0-unversioned"
        assert version.startswith("v")

    def test_sector_version_breakdown_splits_history(self):
        """闭合样本按分类口径版本计数——新旧口径统计不再互相污染"""
        from src.feedback.strategy_stats import sector_version_breakdown
        trades = [
            {"strategy": "价量突破", "pnl_pct": 1.0, "hypothesis": {"sector_version": "v2026-08"}},
            {"strategy": "价量突破", "pnl_pct": -2.0, "hypothesis": {"sector_version": "v2026-08"}},
            {"strategy": "价量突破", "pnl_pct": 3.0, "hypothesis": {"sector_version": "v2026-09-07-a"}},
        ]
        counts = sector_version_breakdown(trades)
        assert counts["v2026-08"] == 2
        assert counts["v2026-09-07-a"] == 1

    def test_unversioned_falls_to_v0(self):
        from src.feedback.strategy_stats import sector_version_breakdown
        counts = sector_version_breakdown([{"strategy": "x", "pnl_pct": 1.0, "hypothesis": {}}])
        assert counts == {"v0-unversioned": 1}


# ============================================================
# P2-2 资金拆层（快变量投票，慢变量单独计票）
# ============================================================

class TestFundLayering:
    def test_fast_sources_only_drive_same_day_vote(self, monkeypatch):
        """两融+1/龙虎榜-1（快）→ 当日票 0；主力/股东拆层不投票"""
        import src.analyzers.institutional_scorer as _inst
        monkeypatch.setattr(_inst, "_fetch_margin_balance",
                            lambda c: {"vote": 1, "detail": "两融增加", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_lhb_institutional",
                            lambda c: {"vote": -1, "detail": "净卖出", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_main_force_flow",
                            lambda c: {"vote": 1, "detail": "主力净流入", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_shareholder_count",
                            lambda c: {"vote": 1, "detail": "户数减少", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_top10_institutional_ratio", lambda c: None)
        _inst._reset_institutional_state()
        result = _REAL_SCORE_INSTITUTIONAL("002975")
        assert result["vote_score"] == 0                    # 快源 1-1
        layering = result["fund_layering"]
        assert layering["enabled"] is True
        assert layering["score"] == 1                       # 慢层 0.5+0.5（展示不投票）
        assert "不参与当日投票" in layering["note"]

    def test_rendering_shows_slow_layer_line(self):
        """④资金行渲染：慢变量计票独立展示"""
        from src.push.templates import _institutional
        data = {
            "institutional_holding": {
                "vote_score": 0, "vote_label": "资金中性",
                "bullish_count": 1, "bearish_count": 1, "neutral_count": 2,
                "label_scope": "资金流投票(4源,非研报共识)",
                "fund_layering": {
                    "enabled": True, "score": -1,
                    "sources": {"main_force": -1, "shareholder": 0},
                },
            },
        }
        text = _institutional(data)
        assert "慢变量计票:主力↓/股东→" in text
        assert "拆层不投票" in text


# ============================================================
# P2-3 组合预算（同板块并发敞口上限）
# ============================================================

class TestPortfolioBudget:
    def _sig(self, code, sector):
        from src.analyzers.timing_engine import EntrySignal
        return EntrySignal(
            stock_code=code, stock_name=f"标的{code}", entry_type="价量突破",
            entry_trigger_price=100.0, stop_loss=95.0,
            target_type="突破持有", target_range=[108.0, 115.0],
            sector_status="main_trend", sector_name=sector,
            benchmark_price=98.0, rrr_low=2.5,
        )

    def test_semiconductor_8_same_label_blocked_beyond_3(self):
        """9/7 半导体设备 8 只同标签 → 前3放行，后续降级观察并留痕"""
        from src.analyzers.portfolio_budget import apply_portfolio_budget
        entries = [self._sig(f"68840{i}", "半导体设备(主题)") for i in range(8)]
        result = apply_portfolio_budget(entries, holdings=[], config={})
        assert result["applied"] is True
        assert len(result["blocked"]) == 5
        assert len(result["passed"]) == 3
        assert result["counts"]["半导体设备"] == 3
        assert "集群风险拦截" in result["blocked"][0]["reason"]
        assert "个股纪律结论保留" in result["blocked"][0]["reason"]

    def test_holdings_count_toward_budget(self):
        """预算计入已有持仓：已有2只同板块 → 新信号只剩1个名额"""
        from src.analyzers.portfolio_budget import apply_portfolio_budget
        holdings = [
            {"stock_code": "688001", "sector_name": "半导体设备(主题)"},
            {"stock_code": "688002", "sector_name": "半导体设备(主题)"},
        ]
        entries = [self._sig(f"68840{i}", "半导体设备(主题)") for i in range(3)]
        result = apply_portfolio_budget(entries, holdings=holdings, config={})
        assert len(result["blocked"]) == 2
        assert len(result["passed"]) == 1

    def test_different_sectors_independent(self):
        from src.analyzers.portfolio_budget import apply_portfolio_budget
        entries = [
            self._sig("688409", "半导体设备(主题)"),
            self._sig("300308", "光模块(CPO)(主题)"),
            self._sig("920045", "通信设备(主题)"),
        ]
        result = apply_portfolio_budget(entries, holdings=[], config={})
        assert len(result["blocked"]) == 0

    def test_disabled_budget_passes_all(self):
        from src.analyzers.portfolio_budget import apply_portfolio_budget
        entries = [self._sig(f"68840{i}", "半导体设备(主题)") for i in range(8)]
        result = apply_portfolio_budget(
            entries, holdings=[], config={"portfolio_budget": {"enabled": False}})
        assert result["applied"] is False
        assert len(result["passed"]) == 8


# ============================================================
# P3-1 趋势延续策略（第五策略）
# ============================================================

def _woerde_kline():
    """沃尔德式延续段：5日前突破后高位整固，昨日回踩，今日再放量"""
    kline = []
    closes = [95.0 + i * 0.3 for i in range(25)]          # 缓慢爬升
    closes += [104.5, 102.0, 100.5, 100.8, 101.2]         # 突破+整固回踩
    for i, close in enumerate(closes):
        kline.append({
            "date": f"2026-08-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + 1.5, "low": close - 1.5,
            "close": close, "volume": 1_000_000,
        })
    return kline


def _woerde_tech(**overrides):
    tech = {
        "current_price": 104.37,            # +7.02%，回到高位
        "recent_high": 104.5,
        "ma5": 101.5, "ma10": 100.0, "ma20": 98.0,
        "ma25": 96.5, "ma25_prev": 96.2, "prev_close": 97.55,
        "volume_ratio": 1.72,
        "kline": _woerde_kline(),
        "tech_signals": {},
        "fundamental": {"profit_yoy": 34.0, "profit_abs": 4.49},
        "institutional_holding": {"vote_score": 0, "votes": {}},
    }
    tech.update(overrides)
    return tech


class TestTrendContinuation:
    def test_woerde_pattern_triggers_fifth_strategy(self, monkeypatch):
        """沃尔德 9/7 +7.02% 延续段 → 趋势延续信号（四策略无一覆盖的缺口）"""
        from src.analyzers.timing_engine import TimingEngine, StopLossCalc
        te = TimingEngine(backtest_mode=True)
        tech = _woerde_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        stop = StopLossCalc(
            stock_code="688028", current_price=104.37,
            support_candidates=[], chosen_support=100.0,
            stop_loss_price=98.0, resistance=110.0,
        )
        sig = te._check_trend_continuation("688028", "沃尔德", tech, stop, "defend", "main_trend")
        assert sig is not None
        assert sig.entry_type == "趋势延续"
        assert "延续段" in sig.trigger_reason
        assert "再度放量" in sig.trigger_reason

    def test_fresh_breakout_day_excluded(self, monkeypatch):
        """突破当日归价量突破（事件边界：昨收在昨MA25下方）→ 本策略不覆盖"""
        from src.analyzers.timing_engine import TimingEngine, StopLossCalc
        te = TimingEngine(backtest_mode=True)
        tech = _woerde_tech(prev_close=95.0, ma25_prev=96.0)   # 昨收 < 昨MA25 = 当日突破
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        stop = StopLossCalc(
            stock_code="688028", current_price=104.37,
            support_candidates=[], chosen_support=100.0, stop_loss_price=98.0, resistance=110.0,
        )
        sig = te._check_trend_continuation("688028", "沃尔德", tech, stop, "defend", "main_trend")
        assert sig is None

    def test_broken_ma10_excluded(self, monkeypatch):
        """回踩破 MA10 = 延续结构破位 → 不入场（Z 逻辑前置一致）"""
        from src.analyzers.timing_engine import TimingEngine, StopLossCalc
        te = TimingEngine(backtest_mode=True)
        tech = _woerde_tech(current_price=99.0)               # < MA10 100
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        stop = StopLossCalc(
            stock_code="688028", current_price=99.0,
            support_candidates=[], chosen_support=100.0, stop_loss_price=98.0, resistance=110.0,
        )
        sig = te._check_trend_continuation("688028", "沃尔德", tech, stop, "defend", "main_trend")
        assert sig is None

    def test_exit_spec_paired_z_is_ma10(self):
        """趋势延续配对出场规格：Z 锚 MA10（延续结构破位），W=延伸目标；
        默认 buffered = MA10−clamp(k×ATR)，裸结构位对照族 Z=MA10 本体"""
        from src.analyzers.hypothesis import STRATEGY_EXIT_SPECS, calculate_paired_stop
        spec = STRATEGY_EXIT_SPECS["趋势延续"]
        assert spec["z_reference"] == "ma10"
        assert spec["w_rule"]
        z, ref, note = calculate_paired_stop(
            "趋势延续", _woerde_tech(), benchmark_price=101.5, atr=2.0)
        assert ref == 100.0
        # ATR/ref = 2% < 5% → 低波动档 k=2.0 → buffer=4.0 → Z=96.0（Phase5 默认）
        assert z == 96.0
        assert "MA10" in note
        z_bare, _, _ = calculate_paired_stop(
            "趋势延续", _woerde_tech(), benchmark_price=101.5, atr=2.0,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}})
        assert z_bare == 100.0                # 裸结构位对照族

    def test_disabled_switch_restores_absence(self, monkeypatch):
        from src.analyzers.timing_engine import TimingEngine, StopLossCalc
        te = TimingEngine(backtest_mode=True)
        original = te._cfg
        te._cfg = lambda *path, default=None: (
            {"enabled": False} if path[:2] == ("trend_continuation",)
            else original(*path, default=default)
        )
        stop = StopLossCalc(
            stock_code="688028", current_price=104.37,
            support_candidates=[], chosen_support=100.0, stop_loss_price=98.0, resistance=110.0,
        )
        sig = te._check_trend_continuation(
            "688028", "沃尔德", _woerde_tech(), stop, "defend", "main_trend")
        assert sig is None


# ============================================================
# P3-2 第八问驱动源
# ============================================================

class TestDriverAttribution:
    def test_bojie_classified_as_earnings_plus_sector(self):
        """博杰 9/7：净利+746.9% + 3C设备主线 → 业绩驱动+板块联动（双标签）"""
        from src.analyzers.driver_attribution import classify_driver
        info = classify_driver(
            {"fundamental": {"profit_yoy": 746.9, "forecast_type": "预增"},
             "volume_ratio": 2.09},
            sector_status="main_trend", sector_name="3C设备(苹果链)",
        )
        assert info["driver"] == "earnings"
        assert info["dual"] == "业绩驱动+板块联动"
        assert "净利同比+746.9%" in info["evidence"]
        assert "3C设备(苹果链)" in info["evidence"]

    def test_wld_classified_as_sector(self):
        """沃尔德：净利+34%（未达业绩线）+ 主线 → 板块联动"""
        from src.analyzers.driver_attribution import classify_driver
        info = classify_driver(
            {"fundamental": {"profit_yoy": 34.0},
             "volume_ratio": 1.72,
             "outer_volume": 396.0, "inner_volume": 296.0},
            sector_status="main_trend", sector_name="通用设备",
        )
        assert info["driver"] == "sector"

    def test_funds_driver_when_no_sector_no_earnings(self):
        from src.analyzers.driver_attribution import classify_driver
        info = classify_driver(
            {"fundamental": {"profit_yoy": 10.0}, "volume_ratio": 1.8,
             "outer_volume": 200.0, "inner_volume": 100.0},
            sector_status="rotational", sector_name="轮动板块",
        )
        assert info["driver"] == "funds"
        assert info["label"] == "资金驱动"

    def test_price_driver_is_honest_fallback(self):
        """无证据 → 价格驱动（系统明说只看到价格表象，不编造叙事）"""
        from src.analyzers.driver_attribution import classify_driver
        info = classify_driver(
            {"fundamental": {"profit_yoy": 5.0}, "volume_ratio": 0.9},
            sector_status="rotational", sector_name="",
        )
        assert info["driver"] == "price"
        assert "不编造叙事" in info["evidence"]

    def test_driver_line_rendered(self):
        from src.analyzers.driver_attribution import classify_driver, driver_line
        info = classify_driver(
            {"fundamental": {"profit_yoy": 746.9}},
            sector_status="main_trend", sector_name="3C设备",
        )
        text = driver_line(info)
        assert "业绩驱动" in text
        # 模板层渲染（买入卡 ⑧驱动源）
        from src.push.templates import _driver_line
        assert "业绩驱动" in _driver_line({"hypothesis": {"driver": info}})


# ============================================================
# P3-3 参数附录
# ============================================================

class TestParamAppendix:
    def test_appendix_lists_all_decision_params(self):
        """附录覆盖决策记录的全部关键参数 + 出处 + 验证状态"""
        from src.feedback.param_appendix import build_param_appendix
        text = build_param_appendix(
            config={},
            stats={"价量突破": {"stats": {"trades": 40, "wins": 18}}},
        )
        assert "RRR门槛1.5" in text
        assert "隐含胜率假设40%" in text
        assert "实测胜率45.0%" in text
        assert "Z线模式:bare_structure" in text
        assert "量能外推" in text
        assert "卖出分级OR" in text
        assert "防守追强试探仓" in text
        assert "风险预算1%÷止损距离" in text
        assert "附录机制永不作废" in text

    def test_appendix_without_stats_is_honest(self):
        from src.feedback.param_appendix import build_param_appendix
        text = build_param_appendix()
        assert "样本不足" in text or "七天未验证" in text


# ============================================================
# 主题版本注入：run_unified_analysis 批次携带（轻量断言，不跑全引擎）
# ============================================================

class TestBatchFields:
    def test_unified_batch_has_phase4_fields(self):
        from src.orchestrator.unified_engine import UnifiedSignalBatch
        batch = UnifiedSignalBatch()
        assert batch.standby_queue == []
        assert batch.chase_missed == []
        assert batch.budget_blocked == []
        assert batch.theme_version == ""
