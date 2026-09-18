# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

import pytest


today = datetime.now().strftime("%Y-%m-%d")
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def test_fund_counts_are_scoped_to_fast_sources(monkeypatch):
    import src.analyzers.institutional_scorer as scorer

    monkeypatch.setitem(scorer._FUND_LAYERING, "enabled", True)
    monkeypatch.setitem(scorer._FUND_LAYERING, "fast_sources", ("north_bound", "lhb"))
    monkeypatch.setitem(scorer._FUND_LAYERING, "slow_sources", ("main_force", "shareholder"))
    monkeypatch.setattr(scorer, "_fetch_margin_balance", lambda code: {
        "vote": 1, "detail": "两融增加",
        "raw": {"as_of": today},
    })
    monkeypatch.setattr(scorer, "_fetch_lhb_institutional", lambda code: {
        "vote": -1, "detail": "净卖出",
        "raw": {"as_of": yesterday},
    })
    monkeypatch.setattr(scorer, "_fetch_main_force_flow", lambda code: {
        "vote": 1, "detail": "主力净流入",
        "raw": {"as_of": today},
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
    # P1-12：事件止损Z 是风险出口，不得再标为“结构位”
    assert "事件跟踪止损:85.54(风险出口)" in text
    assert "技术跟踪止损未触发(现价97.00>90.30)" in text


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


def test_p214_completion_price_labels_source_and_dual_value():
    """P2-14：完结价标注数据源，与交易所收盘差异>0.1元或>0.1% 时双值显示。

    中际旭创 #308 完结价 908.09 vs 交易所收盘 907.80（差 0.29 元）必须双价显示。
    """
    from datetime import date

    from src.analyzers.signal_lifecycle import (
        InMemorySignalEventStore,
        SignalLifecycle,
    )

    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lifecycle.mark_filled(event.event_id)
    lifecycle.mark_signal_target(event.event_id, trigger_data={
        "close": 908.09, "price_source": "现价快照",
        "exchange_close": 907.80, "today": "2026-09-16",
    })
    note = lifecycle.event_status_note("300308", current_price=908.09)
    assert "完结价908.09(现价快照) vs 交易所收盘907.80" in note

    # 完结价与交易所收盘一致 → 单值带口径，无双值
    lc2 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev2 = lc2.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc2.mark_filled(ev2.event_id)
    lc2.mark_signal_target(ev2.event_id, trigger_data={
        "close": 907.80, "price_source": "收盘",
        "exchange_close": 907.80, "today": "2026-09-16",
    })
    note2 = lc2.event_status_note("300308", current_price=907.80)
    assert "完结价907.80(收盘)" in note2
    assert " vs 交易所收盘" not in note2

    # 旧事件无口径字段 → 按键名推断（close→收盘），不崩
    lc3 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev3 = lc3.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc3.mark_filled(ev3.event_id)
    lc3.mark_signal_target(ev3.event_id, trigger_data={
        "close": 908.09, "today": "2026-09-16",
    })
    note3 = lc3.event_status_note("300308", current_price=908.09)
    assert "完结价908.09(收盘)" in note3


def test_p215_completion_reason_is_two_level_enum():
    """P2-15：完结统一为两级枚举——一级(二级)，如 止盈(信号止盈)、作废(已过期)、时间(时间离场)。

    原四类平铺完结术语（追高放弃/时间离场/已过期/已失效）并存，改为两级归属。
    """
    from src.analyzers.signal_lifecycle import (
        InMemorySignalEventStore,
        SignalLifecycle,
    )

    # 止盈：一级=止盈，二级=信号止盈
    lc1 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev1 = lc1.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc1.mark_filled(ev1.event_id)
    lc1.mark_signal_target(ev1.event_id, trigger_data={
        "close": 908.09, "today": "2026-09-16",
    })
    note1 = lc1.event_status_note("300308", current_price=908.09)
    assert "已完结(止盈(信号止盈))" in note1

    # 时间离场：一级=时间，二级=时间离场
    lc2 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev2 = lc2.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc2.mark_filled(ev2.event_id)
    lc2.mark_time_exit(ev2.event_id, trigger_data={
        "close": 890.0, "today": "2026-09-16",
    })
    note2 = lc2.event_status_note("300308", current_price=890.0)
    assert "已完结(时间(时间离场))" in note2

    # 止损：一级=止损，二级=信号止损
    lc3 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev3 = lc3.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc3.mark_filled(ev3.event_id)
    lc3.mark_signal_stop(ev3.event_id, trigger_data={
        "close": 820.0, "today": "2026-09-16",
    })
    note3 = lc3.event_status_note("300308", current_price=820.0)
    assert "已完结(止损(信号止损))" in note3

    # 追高放弃：一级=作废，二级=追高放弃
    lc4 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    ev4 = lc4.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
        target_low=907.0, target_high=915.0,
    )
    lc4.mark_chase_abandon(ev4.event_id, trigger_data={
        "close": 920.0, "today": "2026-09-16",
    })
    note4 = lc4.event_status_note("300308", current_price=920.0)
    assert "已完结(作废(追高放弃))" in note4
