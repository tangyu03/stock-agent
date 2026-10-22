from datetime import UTC, datetime

import pytest

from pointtable.core.lifecycle import TransitionEvent, transition
from pointtable.models.journal import TransitionConfirmation
from pointtable.models.signal import Signal, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy


def _signal() -> Signal:
    return Signal(
        id='evt-1',
        symbol='300001',
        strategy_id='volume_breakout',
        side='BUY',
        tag=SignalTag.ENTER,
        zone_low=10.0,
        zone_high=11.0,
        trend=SignalTrend.UP,
        valid_policy=SignalValidPolicy.UNTIL_STOPPED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_hit_creates_confirmed_journal():
    updated, journal = transition(
        _signal(), TransitionEvent.HIT, 'buy_hit', 10.5, TransitionConfirmation.CONFIRMED
    )
    assert updated.status is SignalStatus.TRIGGERED
    assert updated.closed_at is None
    assert journal.confirmed is TransitionConfirmation.CONFIRMED


def test_terminal_states_close_and_cannot_transition():
    expired, _ = transition(_signal(), TransitionEvent.EOD, now=datetime(2026, 1, 2, tzinfo=UTC))
    assert expired.status is SignalStatus.EXPIRED
    assert expired.closed_at is not None
    with pytest.raises(ValueError, match='terminal'):
        transition(expired, TransitionEvent.HIT)


def test_invalid_human_veto_is_recorded():
    updated, journal = transition(_signal(), TransitionEvent.HUMAN_VETO, 'user_veto')
    assert updated.status is SignalStatus.INVALIDATED
    assert journal.to_status == 'INVALIDATED'


def test_activation_is_copy_only_and_does_not_affect_transitions():
    row = _signal().model_copy(update={'activation': 'only a copy hint'})
    updated, _ = transition(row, TransitionEvent.HIT, 'buy_hit', 10.5)
    assert updated.status is SignalStatus.TRIGGERED
