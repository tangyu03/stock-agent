from __future__ import annotations

from collections.abc import Iterable

from ..models.signal import Signal, SignalStatus, SignalTag


def restore_reminders(signals: Iterable[Signal], close_price: float) -> list[Signal]:
    '''A same-day recovery is a reminder only; it never rewrites the stop state.'''
    return [
        signal
        for signal in signals
        if (
            signal.tag is SignalTag.STOP
            and signal.status is SignalStatus.TRIGGERED
            and signal.zone_low <= close_price <= signal.zone_high
        )
    ]
