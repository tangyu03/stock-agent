from __future__ import annotations

from datetime import datetime

from ..core.lifecycle import JournalEntry, TransitionEvent, transition
from ..core.calendar import trading_days_between
from ..core.volume import BreakQualifier
from ..models.signal import Signal, SignalSide, SignalStatus, SignalTag, SignalValidPolicy


def expire_intraday(signals: list[Signal], now: datetime) -> list[Signal]:
    expired_keys = [
        id(signal)
        for signal in signals
        if signal.status is SignalStatus.ACTIVE
        and signal.tag is not SignalTag.RETEST_CONFIRM
        and (
            signal.valid_policy is SignalValidPolicy.INTRADAY
            or (
                signal.valid_policy is SignalValidPolicy.NEXT_DAY
                and now.date() > signal.created_at.date()
            )
        )
    ]
    return [
        signal.model_copy(update={
            'status': SignalStatus.EXPIRED,
            'status_reason': 'eod',
            'closed_at': now,
        })
        if id(signal) in expired_keys
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
        'role': None,
        'side': SignalSide.NEUTRAL,
        'zone_low': float(retest_zone[0]),
        'zone_high': float(retest_zone[1]),
        'status': SignalStatus.ACTIVE,
        'status_reason': '',
        'valid_policy': SignalValidPolicy.FIXED_WINDOW,
        'created_at': created_at,
        'parent_id': stop_signal.parent_id or stop_signal.id,
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
    passed: bool,
    price: float | None = None,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    event = TransitionEvent.PREMISE_BROKEN if passed else TransitionEvent.HIT
    reason = 'retest_pass' if passed else 'retest_fail'
    return transition(
        retest_signal,
        event,
        reason,
        price,
        now=now,
    )


def stop_break_qualifier(stop_signal: Signal) -> BreakQualifier:
    if stop_signal.break_qualifier:
        try:
            return BreakQualifier(stop_signal.break_qualifier)
        except ValueError:
            pass
    for part in reversed(stop_signal.status_reason.split(':')):
        try:
            return BreakQualifier(part)
        except ValueError:
            continue
    return BreakQualifier.DELAY_OBSERVE


def expire_retest(
    retest_signal: Signal,
    now: datetime,
    window_days: int = 3,
    holidays: set | None = None,
) -> tuple[Signal, JournalEntry] | None:
    if retest_signal.status is not SignalStatus.ACTIVE:
        return None
    if window_days < 0:
        raise ValueError('retest window_days must not be negative')
    elapsed = trading_days_between(
        retest_signal.created_at.date(), now.date(), holidays
    )
    if elapsed < window_days:
        return None
    return transition(
        retest_signal,
        TransitionEvent.EOD,
        'retest_window_expired',
        now=now,
    )


def correction_notice(signal: Signal, trigger: str) -> str:
    if trigger == 'retest_pass':
        return f"{signal.symbol}：放量站回止损区，假破位确认。可按纪律纠错；追不追，是你。"
    return f"{signal.symbol}：收盘收复止损区，当日假破位。仅纠错提醒，不立观察哨。"


def post_break_correction_notice(anchor: dict) -> str:
    return (
        f"{anchor['symbol']}：{anchor['qualifier']} 破位时 {anchor['trade_date']}，"
        f"今日价格反抽回 {anchor['zone_low']:.3g}–{anchor['zone_high']:.3g}。"
        "主动放量破位定性不变；若你当日未走，此处是纠错参考。追不追，是你。"
    )
