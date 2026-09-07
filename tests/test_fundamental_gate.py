"""
【二】基本面闸门 — 回归测试（Phase2-A）

核心案例（用户实测批评）：
  澜起科技 688008：中报"扣非+21% 但净利+72% 靠投资收益" → 盈利质量低
    （warn：降级 + 风险乘数 0.6，不否决——扣非仍在增长）
  汇成真空 301392：业绩雷（预亏/扣非大幅下滑） → veto 出厂拒绝
    （买入理由 X 与基本面不能共存："放量突破"不构成买入理由）
  财报窗口：距法定披露日 ≤7 天 → warn 降级（防披露跳空，不硬否决）
"""
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import src.analyzers.fundamental_gate as fg
from src.analyzers.fundamental_gate import (
    evaluate_fundamental_gate,
    fetch_fundamental_snapshot,
    reset_fundamental_state,
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


def _fund(**overrides):
    data = {
        "code": "688008",
        "name": "澜起科技",
        "report_period": "20260630",
        "announce_date": "2026-08-28",
        "profit_yoy": 72.0,
        "deducted_yoy": 21.0,
        "revenue_yoy": 35.0,
        "forecast_type": "",
        "forecast_change_pct": None,
        "forecast_reason": "",
        "next_report_date": (date.today() + timedelta(days=45)).isoformat(),
        "sources": ["stock_yjkb_em"],
    }
    data.update(overrides)
    return data


# ============================================================
# 闸门评估
# ============================================================

class TestEvaluateFundamentalGate:

    def test_langrun_low_quality_growth_warn(self):
        """澜起案例：净利+72% vs 扣非+21%（差 51pp）→ 盈利质量低（warn）"""
        v = evaluate_fundamental_gate(_fund())
        assert v["verdict"] == "warn"
        assert "low_quality_growth" in v["tags"]
        assert v["risk_multiplier"] == 0.6
        assert any("盈利质量低" in r for r in v["reasons"])
        assert "扣非" in v["note"] and "净利" in v["note"]

    def test_huicheng_forecast_loss_veto(self):
        """汇成真空案例：预告类型预亏 → 业绩雷（veto）"""
        v = evaluate_fundamental_gate(_fund(
            code="301392", name="汇成真空",
            forecast_type="预亏", forecast_change_pct=-60.0,
            profit_yoy=-55.0, deducted_yoy=-58.0,
        ))
        assert v["verdict"] == "veto"
        assert "earnings_bomb" in v["tags"]
        assert any("业绩雷" in r for r in v["reasons"])

    def test_deducted_bomb_below_threshold_veto(self):
        """扣非同比 -55% < -30% 阈值 → 业绩雷（无预告也拦）"""
        v = evaluate_fundamental_gate(_fund(profit_yoy=10.0, deducted_yoy=-55.0))
        assert v["verdict"] == "veto"
        assert "earnings_bomb" in v["tags"]

    def test_report_window_warn(self):
        """距法定披露日 5 天（≤7 天窗口）→ warn + 财报窗口标签"""
        v = evaluate_fundamental_gate(_fund(
            profit_yoy=40.0, deducted_yoy=38.0,
            next_report_date=(date.today() + timedelta(days=5)).isoformat(),
        ))
        assert v["verdict"] == "warn"
        assert "report_window" in v["tags"]
        assert any("财报窗口" in r for r in v["reasons"])

    def test_healthy_growth_passes(self):
        v = evaluate_fundamental_gate(_fund(profit_yoy=50.0, deducted_yoy=45.0))
        assert v["verdict"] == "pass"
        assert v["tags"] == []
        assert v["risk_multiplier"] == 1.0

    def test_missing_data_passes_with_note(self):
        v = evaluate_fundamental_gate(None)
        assert v["verdict"] == "pass"
        assert any("数据缺失" in r for r in v["reasons"])

    def test_gate_disabled_passes(self):
        v = evaluate_fundamental_gate(_fund(profit_yoy=-50.0, deducted_yoy=-55.0),
                                      config={"fundamental_gate": {"enabled": False}})
        assert v["verdict"] == "pass"
        assert v["reasons"] == []

    def test_config_overrides_thresholds(self):
        """阈值可配置：quality_gap_pp 调到 60pp → 澜起 51pp 差距不再触发"""
        v = evaluate_fundamental_gate(
            _fund(), config={"fundamental_gate": {"quality_gap_pp": 60.0}}
        )
        assert v["verdict"] == "pass"


# ============================================================
# 快照拉取（数据源 monkeypatch）
# ============================================================

class TestSnapshotFetch:

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_fundamental_state()
        yield
        reset_fundamental_state()

    def test_snapshot_merges_sources(self, monkeypatch):
        yjkb = {"688008": {
            "股票代码": "688008", "股票简称": "澜起科技",
            "净利润-同比增长": 72.0, "营业收入-同比增长": 35.0,
            "最新公告日期": "2026-08-28 00:00:00",
        }}
        yjyg = {"688008": {
            "股票代码": "688008", "股票简称": "澜起科技",
            "预测类型": "预增", "预告净利润变动幅度": 65.0,
            "业绩变动原因": "内存接口芯片需求旺盛",
        }}

        def fake_table(func_name, period):
            if func_name == "stock_yjkb_em":
                return yjkb
            if func_name == "stock_yjyg_em":
                return yjyg
            return {}

        monkeypatch.setattr(fg, "_fetch_market_table", fake_table)
        monkeypatch.setattr(fg, "_fetch_deducted_yoy", lambda code, period: 21.0)

        snap = fetch_fundamental_snapshot("688008", "澜起科技")
        assert snap is not None
        assert snap["profit_yoy"] == 72.0
        assert snap["deducted_yoy"] == 21.0
        assert snap["forecast_type"] == "预增"
        assert snap["report_period"]
        assert "stock_yjkb_em" in snap["sources"]
        assert "stock_financial_abstract" in snap["sources"]
        assert snap["next_report_date"]

    def test_all_sources_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(fg, "_fetch_market_table", lambda f, p: {})
        monkeypatch.setattr(fg, "_fetch_deducted_yoy", lambda c, p: None)
        assert fetch_fundamental_snapshot("301392", "汇成真空") is None

    def test_session_cache_hit(self, monkeypatch):
        calls = {"n": 0}

        def fake_table(func_name, period):
            calls["n"] += 1
            if func_name == "stock_yjkb_em":
                return {"688008": {"股票代码": "688008", "净利润-同比增长": 72.0}}
            if func_name == "stock_yjyg_em":
                return {"688008": {"股票代码": "688008", "预测类型": "预增"}}
            return {}

        monkeypatch.setattr(fg, "_fetch_market_table", fake_table)
        monkeypatch.setattr(fg, "_fetch_deducted_yoy", lambda c, p: None)
        first = fetch_fundamental_snapshot("688008", "澜起科技")
        second = fetch_fundamental_snapshot("688008", "澜起科技")
        assert first is not None and second is not None
        assert second.get("stale") is True
        # 首次：快报表 1 次 + 预告表 1 次；第二次：全部命中 session 缓存
        assert calls["n"] == 2


# ============================================================
# 执行计划集成（出厂拒绝 / 降级）
# ============================================================

def _plan_kwargs():
    return dict(
        entry_type="价量突破",
        benchmark_price=94.0,
        stop_loss=83.5,
        target_range=[109.0, 116.0],
        tech_data=_tech_data(),
        sector_status="main_trend",
        hypothesis_x="放量站上MA25，突破位回踩不破",
    )


class TestExecutionPlanFundamentalGate:

    def test_bomb_veto_rejects_plan(self):
        tech = _tech_data(fundamental=_fund(
            code="301392", name="汇成真空",
            forecast_type="预亏", profit_yoy=-55.0, deducted_yoy=-58.0,
        ))
        plan = build_execution_plan(**{**_plan_kwargs(), "tech_data": tech})
        assert plan.execute is False
        assert plan.fundamental_rejected is True
        assert plan.hypothesis_rejected is False
        assert any("基本面闸门" in r and "业绩雷" in r for r in plan.rejection_reasons)
        assert any("基本面闸门" in n for n in plan.hard_constraint_notes)
        assert plan.fundamental["verdict"]["verdict"] == "veto"

    def test_low_quality_warn_downgrades_not_rejects(self):
        tech = _tech_data(fundamental=_fund())  # 澜起：净利+72%/扣非+21%
        plan = build_execution_plan(**{**_plan_kwargs(), "tech_data": tech})
        assert plan.execute is True
        assert plan.fundamental_rejected is False
        assert plan.risk_multipliers.get("fundamental_warn") == 0.6
        assert plan.combined_risk_multiplier <= 0.6
        assert any("盈利质量低" in n for n in plan.hard_constraint_notes)
        assert any("基本面降级" in d for d in plan.confidence_details)

    def test_warn_confidence_downgraded_one_notch(self):
        rank = {"低": 0, "中": 1, "高": 2}
        base = build_execution_plan(**_plan_kwargs())
        warn = build_execution_plan(**{**_plan_kwargs(),
                                       "tech_data": _tech_data(fundamental=_fund())})
        assert rank[warn.confidence] <= rank[base.confidence]
        # 至少从"高"跌到"中"（若基准为高）
        if base.confidence == "高":
            assert warn.confidence == "中"

    def test_no_fundamental_unchanged(self):
        plan = build_execution_plan(**_plan_kwargs())
        assert plan.execute is True
        assert plan.fundamental is None
        assert plan.fundamental_rejected is False
        assert plan.risk_multipliers.get("fundamental_warn") is None


# ============================================================
# 引擎出厂拒绝流（汇成真空式业绩雷在生成阶段被拦）
# ============================================================

class TestEngineFundamentalRejection:

    def test_bomb_rejected_at_factory(self, monkeypatch):
        from src.analyzers.timing_engine import TimingEngine
        from src.analyzers.stock_filter import FilterResult

        te = TimingEngine(backtest_mode=False)
        day1 = _tech_data(
            current_price=89.5, today_open=88.8,
            ma25=88.0, ma25_prev=88.5, prev_close=88.2,
            ma5=88.8, ma10=88.6, ma20=87.5,
            volume_ratio=2.0, turnover_rate=4.0,
            recent_high=92.0,
            today_volume=2_000_000, volume_ma60=1_000_000,
            fundamental=_fund(
                code="301392", name="汇成真空",
                forecast_type="预亏", profit_yoy=-55.0, deducted_yoy=-58.0,
            ),
        )
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": day1)
        monkeypatch.setattr(te, "_lifecycle", type(
            "L", (), {
                "get_active_events": lambda self, code: [],
                "register_event": lambda **kw: type("E", (), {"event_id": "ev-test"})(),
            })())
        fr = FilterResult(stock_code="301392", stock_name="汇成真空", passed=True)

        signals = te.check_entry_signals(
            "301392", "汇成真空", "defend",
            sector_status="main_trend", filter_result=fr,
        )
        # 业绩雷：假说/事件再完整，出厂也拒绝
        assert signals == []
        rejection = te._entry_rejections.get("301392")
        assert rejection is not None
        assert rejection.get("fundamental_rejected") is True
        assert any("业绩雷" in r for r in rejection.get("reasons", []))
        assert rejection.get("fundamental", {}).get("forecast_type") == "预亏"
