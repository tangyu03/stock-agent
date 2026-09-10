# -*- coding: utf-8 -*-
from datetime import date

from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore,
    SignalLifecycle,
)


def _make_event(entry_price=498.0, stop_loss=480.0):
    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "920045", "蘅东光", "确认追强",
        breakout_level=490.0, entry_price=entry_price,
        stop_loss=stop_loss, target_low=560.0, target_high=580.0,
    )
    return lifecycle, event


def test_bearish_volume_day_freezes_triggered_event():
    lifecycle, event = _make_event()
    notices = lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )

    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "frozen"
    assert saved.frozen_prev_status == "valid"
    assert any(n["exit_type"] == "事件冻结" for n in notices)
    assert "已冻结" in lifecycle.event_status_note("920045", current_price=499.0)
    assert "待回踩" not in lifecycle.event_status_note("920045", current_price=499.0)
    assert lifecycle.store.logs[-1]["rule_entry"] == "R4.1事件冻结"


def test_negative_score_without_volume_does_not_freeze():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-1.0, volume_ratio=1.0, change_pct=-5.2,
        today=date(2026, 9, 9),
    )

    assert lifecycle.store.events[event.event_id].status == "valid"


def test_frozen_event_unfreezes_when_score_recovers():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    notices = lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=0.0, volume_ratio=0.9, change_pct=1.0,
        today=date(2026, 9, 10),
    )

    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "valid"
    assert any(n["exit_type"] == "事件解冻" for n in notices)
    assert "买点上方待回踩" in lifecycle.event_status_note("920045", current_price=505.0)
    assert lifecycle.store.logs[-1]["rule_entry"] == "R4.2事件解冻"


def test_filled_event_restores_filled_after_unfreeze():
    lifecycle, event = _make_event()
    lifecycle.mark_filled(
        event.event_id,
        trigger_data={"day_low": 498.0, "entry_price": 498.0},
    )
    lifecycle.evaluate_events(
        "920045", current_price=500.0,
        tech_score=-1.5, volume_ratio=1.2, change_pct=-2.0,
        today=date(2026, 9, 9),
    )
    lifecycle.evaluate_events(
        "920045", current_price=500.0,
        tech_score=0.5, volume_ratio=1.0, change_pct=1.0,
        today=date(2026, 9, 10),
    )

    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "filled"
