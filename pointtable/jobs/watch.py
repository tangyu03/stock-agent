from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..core.generator import order_for_push
from ..core.lifecycle import JournalEntry, TransitionConfirmation, TransitionEvent, transition
from ..core.volume import Activity, BreakQualifier, active_or_passive, break_qualifier
from ..models.signal import Signal, SignalSide, SignalStatus, SignalTag


def confirm_touch(
    signal: Signal,
    price: float,
    second_cycle: bool,
    volume_ratio: float | None = None,
) -> tuple[Signal | None, str]:
    in_zone = signal.zone_low <= price <= signal.zone_high
    if not in_zone:
        return None, 'not_in_zone'
    if signal.side is SignalSide.SELL or signal.tag is SignalTag.STOP:
        return signal, 'stop_single_point'
    if second_cycle or (volume_ratio is not None and volume_ratio >= 1.2):
        return signal, 'buy_confirmed'
    return None, 'waiting_second_cycle'


def watch_signals(signals: Iterable[Signal], price: float, second_cycle: bool) -> list[Signal]:
    active = [signal for signal in signals if signal.status is SignalStatus.ACTIVE]
    return order_for_push(active)


def qualify_break(
    *,
    volume_ratio: float,
    active_delta: float,
    stock_triggered: bool,
    sector_triggered: bool,
    index_triggered: bool,
) -> BreakQualifier:
    decision = break_qualifier(volume_ratio, active_delta)
    if decision.qualifier is BreakQualifier.EXECUTE_NOW:
        return BreakQualifier.EXECUTE_NOW
    activity = active_or_passive(stock_triggered, sector_triggered, index_triggered)
    if activity is Activity.ACTIVE:
        return BreakQualifier.EXECUTE_NOW
    return BreakQualifier.DELAY_OBSERVE


def process_touch(
    signal: Signal,
    price: float,
    second_cycle: bool,
    volume_ratio: float | None = None,
    now: datetime | None = None,
) -> tuple[Signal | None, JournalEntry | None, str]:
    touched, reason = confirm_touch(signal, price, second_cycle, volume_ratio)
    if touched is None:
        return None, None, reason
    confirmation = (
        TransitionConfirmation.CONFIRMED
        if signal.side is SignalSide.BUY
        else TransitionConfirmation.TOUCHED_ONLY
    )
    updated, journal = transition(
        signal, TransitionEvent.HIT, reason, price, confirmation, now
    )
    return updated, journal, reason


def invalidate_premise(
    signal: Signal,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    return transition(signal, TransitionEvent.PREMISE_BROKEN, now=now)
