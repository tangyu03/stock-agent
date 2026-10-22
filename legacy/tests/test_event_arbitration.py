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
    assert saved.status == "invalidated"
    assert "冻结即同步撤单" in saved.invalid_reason
    assert any(n["exit_type"] == "冻结撤单" for n in notices)
    assert "冻结即同步撤单" in lifecycle.event_status_note("920045", current_price=499.0)
    assert "待回踩" not in lifecycle.event_status_note("920045", current_price=499.0)
    assert lifecycle.store.logs[-1]["rule_entry"] == "R3.13冻结同步撤单"


def test_bearish_volume_with_capitulation_evidence_does_not_freeze():
    """恐慌出清结构（长下影/缩量企稳/锤子线）→ 放量阴线不冻结，转低吸观察。"""
    lifecycle, event = _make_event()
    notices = lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        capitulation_evidence=True,
        today=date(2026, 9, 9),
    )
    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "valid"
    assert any(n["exit_type"] == "低吸观察" for n in notices)
    assert not any(n["exit_type"] == "冻结撤单" for n in notices)


def test_negative_score_without_volume_does_not_freeze():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-1.0, volume_ratio=1.0, change_pct=-5.2,
        today=date(2026, 9, 9),
    )

    assert lifecycle.store.events[event.event_id].status == "valid"


def test_frozen_event_does_not_reactivate_without_reassessment():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=0.0, volume_ratio=0.9, change_pct=1.0,
        today=date(2026, 9, 10),
    )

    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "invalidated"
    assert "冻结即同步撤单" in lifecycle.event_status_note("920045", current_price=505.0)


