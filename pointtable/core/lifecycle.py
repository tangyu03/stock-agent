from __future__ import annotations

from datetime import datetime
from enum import Enum

from ..models.journal import JournalEntry, TransitionConfirmation
from ..models.signal import Signal, SignalStatus


class TransitionEvent(str, Enum):
    HIT = 'hit'
    EOD = 'eod'
    PREMISE_BROKEN = 'premise_broken'
    HUMAN_VETO = 'human_veto'
    RETEST_PASS = 'retest_pass'
    RETEST_FAIL = 'retest_fail'
    AUCTION_WEAK = 'auction_weak'
    LHB_VETO = 'lhb_veto'


_TRANSITIONS = {
    TransitionEvent.HIT: SignalStatus.TRIGGERED,
    TransitionEvent.EOD: SignalStatus.EXPIRED,
    TransitionEvent.PREMISE_BROKEN: SignalStatus.INVALIDATED,
    TransitionEvent.HUMAN_VETO: SignalStatus.INVALIDATED,
    TransitionEvent.RETEST_PASS: SignalStatus.INVALIDATED,
    TransitionEvent.RETEST_FAIL: SignalStatus.TRIGGERED,
    TransitionEvent.AUCTION_WEAK: SignalStatus.INVALIDATED,
    TransitionEvent.LHB_VETO: SignalStatus.INVALIDATED,
}


def transition(
    signal: Signal,
    event: TransitionEvent,
    reason: str = '',
    price: float | None = None,
    confirmed: TransitionConfirmation = TransitionConfirmation.TOUCHED_ONLY,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    if signal.status in {SignalStatus.EXPIRED, SignalStatus.INVALIDATED}:
        raise ValueError('terminal signal cannot transition')
    if price is not None and price < 0:
        raise ValueError('price must not be negative')
    timestamp = now or datetime.now()
    target = _TRANSITIONS[event]
    terminal = target in {SignalStatus.TRIGGERED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
    updated = signal.model_copy(
        update={
            'status': target,
            'status_reason': reason or event.value,
            'closed_at': timestamp if terminal else signal.closed_at,
        }
    )
    journal = JournalEntry(
        signal_id=signal.id,
        from_status=signal.status.value,
        to_status=target.value,
        reason=reason or event.value,
        price=price,
        confirmed=confirmed,
        created_at=timestamp,
    )
    return updated, journal
