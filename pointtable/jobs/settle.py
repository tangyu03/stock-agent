from __future__ import annotations

from datetime import date, datetime

from ..core.lifecycle import JournalEntry, TransitionEvent, transition
from ..core.calendar import trading_days_between
from ..core.volume import BreakQualifier, SettlementOutcome
from ..models.signal import Signal, SignalRole, SignalSide, SignalStatus, SignalTag, SignalValidPolicy
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any
from ..db.session import (
    load_signals, save_correction_anchor, save_correction_event, save_journal,
    save_signal, save_zone_snapshot,
)
from ..models.journal import JournalRecordType


def expire_intraday(signals: list[Signal], now: datetime) -> list[Signal]:
    expired_keys = [
        id(signal)
        for signal in signals
        if signal.status is SignalStatus.ACTIVE
        and signal.tag is not SignalTag.RETEST_CONFIRM
        and (
            signal.valid_policy is SignalValidPolicy.INTRADAY
            or signal.valid_policy is SignalValidPolicy.SAME_DAY
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
    return f"{signal.symbol}：收盘收复止损区下沿，当日假破位。仅纠错提醒，不立观察哨。"


def post_break_correction_notice(anchor: dict) -> str:
    return (
        f"{anchor['symbol']}：{anchor['qualifier']} 破位时 {anchor['trade_date']}，"
        f"今日价格反抽回 {anchor['zone_low']:.3g}–{anchor['zone_high']:.3g}。"
        "主动放量破位定性不变；若你当日未走，此处是纠错参考。追不追，是你。"
    )
def observation_pause_symbols(conn: sqlite3.Connection) -> set[str]:
    paused = {
        str(row['symbol'])
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM signals WHERE tag='retest_confirm' AND status='ACTIVE'"
        )
    }
    terminal_retests = conn.execute(
        '''
        SELECT symbol, MAX(closed_at) AS closed_at
        FROM signals
        WHERE tag='retest_confirm' AND status IN ('TRIGGERED','EXPIRED','INVALIDATED')
        GROUP BY symbol
        '''
    ).fetchall()
    for row in terminal_retests:
        symbol = str(row['symbol'])
        active_stop = conn.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND tag='stop' AND status='ACTIVE' LIMIT 1",
            (symbol,),
        ).fetchone()
        triggered_enter = conn.execute(
            '''
            SELECT 1 FROM signals
            WHERE symbol=? AND tag='enter' AND status='TRIGGERED' AND created_at > ?
            LIMIT 1
            ''',
            (symbol, row['closed_at'] or ''),
        ).fetchone()
        if active_stop is None and triggered_enter is None:
            paused.add(symbol)
    return paused



def roll_stops(signals: list[Signal], stop_zones: Mapping[str, Sequence[float]]) -> list[Signal]:
    rolled_by_id: dict[str, Signal] = {}
    for signal in signals:
        zone = stop_zones.get(signal.symbol)
        if (
            signal.tag is not SignalTag.STOP
            or signal.valid_policy is not SignalValidPolicy.UNTIL_STOPPED
            or signal.status is not SignalStatus.ACTIVE
            or zone is None
        ):
            continue
        zone_low, zone_high = float(zone[0]), float(zone[1])
        if zone_low >= zone_high:
            raise ValueError(f'invalid stop zone for {signal.symbol}')
        rolled_by_id[signal.id] = signal.model_copy(update={
            'zone_low': zone_low,
            'zone_high': zone_high,
        })
    return [
        rolled_by_id.get(signal.id, signal)
        for signal in signals
    ]



def run_settle(
    conn: sqlite3.Connection,
    now: datetime,
    stop_zones: Mapping[str, Sequence[float]] | None = None,
    close_prices: Mapping[str, float] | None = None,
    low_prices: Mapping[str, float] | None = None,
    high_prices: Mapping[str, float] | None = None,
    volume_ratios: Mapping[str, float] | None = None,
    break_contexts: Mapping[str, Mapping[str, Any]] | None = None,
    retest_window_days: int = 3,
    retest_volume_min: float = 1.2,
    holidays: set | None = None,
    market_tier: str = 'NORMAL',
    index_trend: str = 'UP',
    created_before: date | None = None,
) -> dict[str, Any]:
    all_signals = load_signals(conn)
    if created_before is not None:
        all_signals = [
            signal for signal in all_signals
            if signal.created_at.date() < created_before
        ]
    original_signals = {signal.id: signal for signal in all_signals}
    for signal in [item for item in all_signals if item.status is SignalStatus.ACTIVE]:
        if signal.signal_role not in {SignalRole.STOP, SignalRole.PROFIT, SignalRole.T}:
            continue
        touched_price = None
        if signal.side is SignalSide.BUY and float((low_prices or {}).get(signal.symbol, 0)) <= signal.zone_high:
            touched_price = signal.zone_high
        elif signal.side is SignalSide.SELL and float((high_prices or {}).get(signal.symbol, 0)) >= signal.zone_low:
            touched_price = signal.zone_low
        if touched_price is None:
            continue
        from .watch import stop_break_decision

        qualifier, break_reason = stop_break_decision((break_contexts or {}).get(signal.symbol))
        signal, journal = transition(
            signal,
            TransitionEvent.HIT,
            'settle_close_touch',
            touched_price,
            now=now,
        )
        signal = signal.model_copy(update={
            'break_qualifier': qualifier.value,
            'status_reason': f'settle_close_touch:{qualifier.value}:{break_reason}',
        })
        journal = journal.model_copy(update={
            'reason': f'settle_close_touch:{qualifier.value}:{break_reason}',
        })
        save_signal(conn, signal)
        save_journal(conn, journal)
    all_signals = load_signals(conn)
    if created_before is not None:
        all_signals = [
            signal for signal in all_signals
            if signal.created_at.date() < created_before
        ]
    original_signals.update({signal.id: signal for signal in all_signals})
    all_signals = roll_stops(all_signals, stop_zones or {})
    active_signals = [signal for signal in all_signals if signal.status is SignalStatus.ACTIVE]
    expired_rows = expire_intraday(active_signals, now)
    expired = [signal for signal in expired_rows if signal.status is SignalStatus.EXPIRED]
    for signal in expired:
        save_signal(conn, signal)
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status=SignalStatus.ACTIVE.value,
            to_status=SignalStatus.EXPIRED.value,
            reason='eod',
            created_at=now,
            ))

    rolled = [
        signal for signal in all_signals
        if stop_zones and signal.tag is SignalTag.STOP
        and signal.valid_policy is SignalValidPolicy.UNTIL_STOPPED
        and signal.status is SignalStatus.ACTIVE
        and signal.symbol in stop_zones
    ]
    for signal in rolled:
        save_signal(conn, signal)
        original = original_signals.get(signal.id)
        if original and (original.zone_low, original.zone_high) != (signal.zone_low, signal.zone_high):
            save_zone_snapshot(conn, signal.id, signal.zone_low, signal.zone_high, now)
            save_journal(conn, JournalEntry(
                signal_id=signal.id,
                from_status=original.status.value,
                to_status=signal.status.value,
                reason='zone_update',
                price=signal.zone_high,
                created_at=now,
                record_type=JournalRecordType.ZONE_UPDATE,
            ))

    existing_ids = {signal.id for signal in load_signals(conn)}
    created_retests: list[str] = []
    settled_retests: list[str] = []
    expired_retests: list[str] = []
    correction_notices: list[str] = []
    
    for stop in [
        signal for signal in all_signals
        if signal.tag.value == 'stop'
        and signal.status is SignalStatus.TRIGGERED
        and signal.closed_at is not None
        and signal.closed_at.date() == now.date()
    ]:
        if stop.break_qualifier:
            qualifier = BreakQualifier(stop.break_qualifier)
        elif stop.symbol in (break_contexts or {}):
            context = (break_contexts or {})[stop.symbol]
            from .watch import stop_break_decision

            qualifier, _ = stop_break_decision(context)
        else:
            qualifier = stop_break_qualifier(stop)
        close_price = (close_prices or {}).get(stop.symbol)
        if qualifier is not None and qualifier.value == 'LIMIT_EXEMPT':
            continue
        if qualifier.value == 'EXECUTE_NOW':
            updated_stop = stop.model_copy(update={
                'status_reason': f'settle_execute_now:{stop.status_reason}',
                'settlement_outcome': SettlementOutcome.STILL_BROKEN.value,
            })
            save_signal(conn, updated_stop)
            save_correction_anchor(
                conn,
                signal_id=stop.id,
                symbol=stop.symbol,
                trade_date=now.date().isoformat(),
                qualifier=qualifier.value,
                zone_low=stop.zone_low,
                zone_high=stop.zone_high,
                created_at=now,
            )
            continue
        if close_price is None:
            raise ValueError(
                f'missing close price for delayed break settlement: {stop.symbol}'
            )
        if close_price >= stop.zone_low:
            updated_stop = stop.model_copy(update={
                'settlement_outcome': SettlementOutcome.SAME_DAY_RECOVERED.value,
                'status_reason': f'settle_close_recovered:{stop.status_reason}',
            })
            save_signal(conn, updated_stop)
            notice = correction_notice(stop, 'same_day_close')
            inserted = save_correction_event(
                conn,
                trade_date=now.date().isoformat(),
                signal_id=stop.id,
                symbol=stop.symbol,
                trigger='same_day_close',
                notice=notice,
                created_at=now,
            )
            if inserted:
                correction_notices.append(notice)
            continue
        if market_tier == 'LOW' and index_trend == 'DOWN':
            updated_stop = stop.model_copy(update={
                'settlement_outcome': SettlementOutcome.WEAK_MARKET_EXECUTE.value,
                'status_reason': f'settle_weak_market_execute:{stop.status_reason}',
            })
            save_signal(conn, updated_stop)
            save_correction_anchor(
                conn,
                signal_id=stop.id,
                symbol=stop.symbol,
                trade_date=now.date().isoformat(),
                qualifier=BreakQualifier.EXECUTE_NOW.value,
                zone_low=stop.zone_low,
                zone_high=stop.zone_high,
                created_at=now,
            )
            continue
        retest = create_retest_confirm(
            stop, (stop.zone_low, stop.zone_high), now
        )
        if retest.id in existing_ids:
            continue
        save_signal(conn, retest)
        existing_ids.add(retest.id)
        created_retests.append(retest.id)

    prices = close_prices or {}
    retest_candidates = load_signals(conn)
    if created_before is not None:
        retest_candidates = [
            signal for signal in retest_candidates
            if signal.created_at.date() < created_before
        ]
    for retest in [
        signal for signal in retest_candidates
        if signal.tag is SignalTag.RETEST_CONFIRM and signal.status is SignalStatus.ACTIVE
    ]:
        price = prices.get(retest.symbol)
        volume_ratio = (volume_ratios or {}).get(retest.symbol)
        settled = False
        if price is not None and price >= retest.zone_high:
            if volume_ratio is not None and volume_ratio >= retest_volume_min:
                updated, journal = settle_retest(retest, True, price, now)
                save_signal(conn, updated)
                save_journal(conn, journal)
                settled_retests.append(retest.id)
                notice = correction_notice(updated, 'retest_pass')
                inserted = save_correction_event(
                    conn,
                    trade_date=now.date().isoformat(),
                    signal_id=updated.id,
                    symbol=updated.symbol,
                    trigger='retest_pass',
                    notice=notice,
                    created_at=now,
                )
                correction_notices.append(notice)
                settled = True
        elif price is not None and retest.zone_low <= price < retest.zone_high:
            updated, journal = settle_retest(retest, False, price, now)
            save_signal(conn, updated)
            save_journal(conn, journal)
            settled_retests.append(retest.id)
            settled = True
        if settled:
            continue
        
        expired_retest = expire_retest(retest, now, retest_window_days, holidays)
        if expired_retest is not None:
            expired_signal, journal = expired_retest
            save_signal(conn, expired_signal)
            save_journal(conn, journal)
            expired_retests.append(retest.id)
    conn.commit()
    return {
        'expired': [signal.id for signal in expired],
        'rolled': [signal.id for signal in rolled],
        'retest_created': created_retests,
        'retest_settled': settled_retests,
        'retest_expired': expired_retests,
        'correction_notices': correction_notices,
    }
