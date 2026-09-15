# -*- coding: utf-8 -*-
import pytest


def test_fund_counts_are_scoped_to_fast_sources(monkeypatch):
    import src.analyzers.institutional_scorer as scorer

    monkeypatch.setitem(scorer._FUND_LAYERING, "enabled", True)
    monkeypatch.setitem(scorer._FUND_LAYERING, "fast_sources", ("north_bound", "lhb"))
    monkeypatch.setitem(scorer._FUND_LAYERING, "slow_sources", ("main_force", "shareholder"))
    monkeypatch.setattr(scorer, "_fetch_margin_balance", lambda code: {
        "vote": 1, "detail": "两融增加",
        "raw": {"as_of": "2026-09-11"},
    })
    monkeypatch.setattr(scorer, "_fetch_lhb_institutional", lambda code: {
        "vote": -1, "detail": "净卖出",
        "raw": {"as_of": "2026-09-10"},
    })
    monkeypatch.setattr(scorer, "_fetch_main_force_flow", lambda code: {
        "vote": 1, "detail": "主力净流入",
        "raw": {"as_of": "2026-09-11"},
    })
    monkeypatch.setattr(scorer, "_fetch_shareholder_count", lambda code: {
        "vote": 1, "detail": "户数减少",
        "raw": {"as_of": "2026-06-30", "age_days": 73},
    })
    monkeypatch.setattr(scorer, "_fetch_top10_institutional_ratio", lambda code: None)
    scorer._reset_institutional_state()
    result = scorer._REAL_INSTITUTIONAL_SCORING("002975", turnover_available=True)

    assert result["vote_score"] == 0
    assert result["bullish_count"] == 1
    assert result["bearish_count"] == 1
    assert result["valid_vote_sources"] == 2
    assert result["covered_sources"] == 4
    assert result["voting_source_scope"] == "快源"


def test_fund_display_includes_coverage_and_scope():
    from src.push.templates import _institutional

    text = _institutional({
        "institutional_holding": {
            "vote_score": 0, "vote_label": "资金中性",
            "bullish_count": 1, "bearish_count": 1,
            "valid_vote_sources": 2, "covered_sources": 4,
            "label_scope": "资金流投票",
        },
    })
    assert "多1/空1" in text
    assert "有效票源2/4" in text
    assert "覆盖4/4" in text


def test_technical_score_detail_sums_to_score():
    from src.push.templates import _tech

    text = _tech({
        "tech_signals": {
            "vote": "偏多", "vote_score": 3.0,
            "vote_details": ["明细占位"],
            "category_votes": {
                "trend": {"vote": 1, "weight": 1.0, "details": ["方向"]},
                "momentum": {"vote": 1, "weight": 1.0, "details": ["时机"]},
                "pattern": {"vote": 1, "weight": 1.0, "details": ["结构"]},
                "volume": {"vote": 0, "weight": 1.0, "details": ["中性不投票"]},
            },
        },
    })
    assert "评分明细: ①方向+1.0 ②时机+1.0 ③结构+1.0 ④量能+0.0" in text


def test_risk_stop_discloses_event_z_and_tracking_line():
    from src.analyzers.timing_engine import StopLossCalc, TimingEngine
    from src.analyzers.signal_lifecycle import SignalEvent

    engine = TimingEngine(backtest_mode=True)
    engine._lifecycle.get_active_events = lambda code: [
        SignalEvent(
            event_id="evt-z", stock_code=code, stock_name="测试",
            entry_type="价量突破", entry_price=102.77,
            stop_loss=85.54, status="filled",
        ),
    ]
    text = engine._risk_stop_text(
        "002975", 97.0,
        StopLossCalc(
            stock_code="002975", current_price=97.0,
            support_candidates=[], chosen_support=93.0,
            stop_loss_price=90.30, resistance=105.0,
        ),
    )
    assert "事件止损Z: 85.54(结构位)" in text
    assert "跟踪止损未触发(现价97.00>90.30)" in text


def test_defensive_gate_summary_folds_to_top3():
    from src.orchestrator.unified_engine import _defensive_gate_blocker_text

    tech = {
        "current_price": 80.0,
        "recent_high": 100.0,
        "prior_high": 100.0,
        "volume_ratio": 0.8,
        "adx": 18.0,
        "rsi": 40.0,
        "kline": [],
        "fundamental": {},
    }
    text = _defensive_gate_blocker_text(tech, "main_trend")
    assert "达标" in text
    assert "缺口前三: " in text
    assert text.count("|", text.index("缺口前三:")) <= 3
