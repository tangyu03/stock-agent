from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ..core.generator import order_for_push
from ..core.lifecycle import JournalEntry, TransitionConfirmation, TransitionEvent, transition
from ..core.volume import Activity, BreakQualifier, active_or_passive, break_qualifier
from ..models.signal import Signal, SignalSide, SignalStatus, SignalTag


def confirm_touch(
    signal: Signal,
    price: float,
    second_cycle: bool,
    volume_ratio: float | None = None,
    retest_volume_min: float = 1.2,
) -> tuple[Signal | None, str]:
    if signal.tag is SignalTag.RETEST_CONFIRM:
        if (
            price >= signal.zone_high
            and volume_ratio is not None
            and volume_ratio >= retest_volume_min
        ):
            return signal, 'retest_pass'
        if signal.zone_low <= price < signal.zone_high:
            return signal, 'retest_fail'
        return None, 'not_in_zone'
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
    divergence: bool = False,
    limit_up: bool = False,
) -> BreakQualifier:
    decision = break_qualifier(volume_ratio, active_delta, divergence, limit_up)
    if decision.qualifier is BreakQualifier.LIMIT_EXEMPT:
        return BreakQualifier.LIMIT_EXEMPT
    if decision.qualifier is BreakQualifier.DIVERGENCE:
        return BreakQualifier.DIVERGENCE
    if decision.qualifier is BreakQualifier.EXECUTE_NOW:
        return BreakQualifier.EXECUTE_NOW
    activity = active_or_passive(stock_triggered, sector_triggered, index_triggered)
    if activity is Activity.ACTIVE:
        return BreakQualifier.EXECUTE_NOW
    return BreakQualifier.DELAY_OBSERVE


def stop_break_decision(context: Mapping[str, Any] | None) -> tuple[BreakQualifier, str]:
    if not context:
        return BreakQualifier.DELAY_OBSERVE, 'NOT_CLASSIFIED'
    decision = break_qualifier(
        float(context.get('volume_ratio') or 0),
        float(context.get('active_delta') or 0),
        bool(context.get('divergence', False)),
        bool(context.get('limit_up', False)),
    )
    if decision.qualifier is BreakQualifier.EXECUTE_NOW:
        return decision.qualifier, decision.reason
    if decision.qualifier in {BreakQualifier.LIMIT_EXEMPT, BreakQualifier.DIVERGENCE}:
        return decision.qualifier, decision.reason
    activity = active_or_passive(
        bool(context.get('stock_triggered', False)),
        bool(context.get('sector_triggered', False)),
        bool(context.get('index_triggered', False)),
    )
    return (
        BreakQualifier.EXECUTE_NOW if activity is Activity.ACTIVE else BreakQualifier.DELAY_OBSERVE,
        decision.reason,
    )


def process_touch(
    signal: Signal,
    price: float,
    second_cycle: bool,
    volume_ratio: float | None = None,
    now: datetime | None = None,
    retest_volume_min: float = 1.2,
) -> tuple[Signal | None, JournalEntry | None, str]:
    touched, reason = confirm_touch(
        signal, price, second_cycle, volume_ratio, retest_volume_min
    )
    if touched is None:
        return None, None, reason
    confirmation = (
        TransitionConfirmation.CONFIRMED
        if signal.side is SignalSide.BUY
        else TransitionConfirmation.TOUCHED_ONLY
    )
    event = (
        TransitionEvent.PREMISE_BROKEN
        if reason == 'retest_pass'
        else TransitionEvent.HIT
    )
    updated, journal = transition(
        signal, event, reason, price, confirmation, now
    )
    return updated, journal, reason


def invalidate_premise(
    signal: Signal,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    return transition(signal, TransitionEvent.PREMISE_BROKEN, now=now)
