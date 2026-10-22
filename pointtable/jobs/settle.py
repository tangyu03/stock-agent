from __future__ import annotations

from datetime import datetime

from ..core.lifecycle import JournalEntry, TransitionConfirmation, TransitionEvent, transition
from ..models.signal import Signal, SignalSide, SignalStatus, SignalTag, SignalValidPolicy


def expire_intraday(signals: list[Signal], now: datetime) -> list[Signal]:
    return [
        signal.model_copy(update={
            'status': SignalStatus.EXPIRED,
            'status_reason': 'eod',
            'closed_at': now,
        })
        if signal.status is SignalStatus.ACTIVE and signal.valid_policy is SignalValidPolicy.INTRADAY
        and signal.tag is not SignalTag.RETEST_CONFIRM
        else signal
        for signal in signals
    ]


def create_retest_confirm(
    stop_signal: Signal,
    retest_zone: tuple[float, float],
    created_at: datetime,
) -> Signal:
    return stop_signal.model_copy(update={
        'id': f'{stop_signal.id}-retest',
        'tag': SignalTag.RETEST_CONFIRM,
        'side': stop_signal.side,
        'zone_low': float(retest_zone[0]),
        'zone_high': float(retest_zone[1]),
        'status': SignalStatus.ACTIVE,
        'status_reason': '',
        'valid_policy': SignalValidPolicy.INTRADAY,
        'created_at': created_at,
        'parent_id': stop_signal.id,
        'closed_at': None,
    })


def apply_trend_flip(
    signals: list[Signal],
    stop_zone: tuple[float, float],
    now: datetime,
) -> list[Signal]:
    updated: list[Signal] = []
    for signal in signals:
        if signal.tag is SignalTag.STOP:
            updated.append(signal.model_copy(update={
                'zone_low': float(stop_zone[0]),
                'zone_high': float(stop_zone[1]),
            }))
        elif signal.tag is SignalTag.T and signal.side is SignalSide.BUY:
            updated.append(signal.model_copy(update={
                'status': SignalStatus.INVALIDATED,
                'status_reason': 'trend_flip',
                'closed_at': now,
            }))
        else:
            updated.append(signal)
    return updated


def settle_retest(
    retest_signal: Signal,
    recovered: bool,
    price: float | None = None,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    event = TransitionEvent.RETEST_FAIL if recovered else TransitionEvent.RETEST_PASS
    reason = 'retest_fail' if recovered else 'retest_pass'
    return transition(
        retest_signal,
        event,
        reason,
        price,
        TransitionConfirmation.CONFIRMED,
        now,
    )
