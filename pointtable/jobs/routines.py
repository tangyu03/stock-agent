from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.auction import AuctionPattern
from ..core.lifecycle import JournalEntry
from ..db.session import (
    load_context_marks,
    save_context_marks,
    save_journal,
    save_signal,
    save_signals,
    touch_context_mark,
    save_event_calendar,
)
from ..models.context_mark import ContextType
from ..models.journal import JournalRecordType
from ..models.signal import Signal, SignalStatus, SignalTag, SignalValidPolicy
from ..stats import signal_quality
from .auction_call import apply_auction
from .evening import build_context_marks, build_event_calendar, build_evening_signals, build_holding_rows
from .plan_builder import build_pool_plans
from .restore_check import restore_reminders
from .settle import create_retest_confirm, expire_intraday
from .settle import settle_retest
from .watch import BreakQualifier, invalidate_premise, process_touch, stop_break_decision


def protection_pause_symbols(conn: sqlite3.Connection) -> set[str]:
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


def context_from_marks(conn: sqlite3.Connection, symbol: str, now: datetime) -> dict[str, bool]:
    threshold = now - timedelta(minutes=30)
    trade_date = now.date().isoformat()
    pool_row = conn.execute(
        '''
        SELECT theme_label FROM pool
        WHERE date <= ? AND symbol=?
        ORDER BY date DESC LIMIT 1
        ''',
        (trade_date, symbol),
    ).fetchone()

    def mark_triggered(context_type: ContextType, code: str) -> bool:
        row = conn.execute(
            '''
            SELECT 1 FROM context_marks
            WHERE date=? AND context_type=? AND code=?
              AND triggered_at IS NOT NULL AND triggered_at >= ?
            LIMIT 1
            ''',
            (trade_date, context_type.value, code, threshold.isoformat(timespec='seconds')),
        ).fetchone()
        return row is not None

    sector_code = str(pool_row['theme_label']) if pool_row else ''
    return {
        'stock_triggered': False,
        'sector_triggered': bool(sector_code and mark_triggered(ContextType.SECTOR, sector_code)),
        'index_triggered': mark_triggered(ContextType.INDEX, 'sh000001'),
    }


def signal_from_row(row: sqlite3.Row | dict[str, Any]) -> Signal:
    item = dict(row)
    if isinstance(item.get('premise'), str):
        try:
            item['premise'] = json.loads(item['premise'])
        except json.JSONDecodeError:
            item['premise'] = {}
    return Signal.model_validate(item)


def load_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    sql = 'SELECT * FROM signals'
    params: tuple[str, ...] = ()
    if trade_date:
        sql += ' WHERE created_at LIKE ?'
        params = (trade_date + '%',)
    sql += ' ORDER BY created_at, id'
    return [signal_from_row(row) for row in conn.execute(sql, params).fetchall()]


def load_active_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    return [signal for signal in load_signals(conn, trade_date) if signal.status is SignalStatus.ACTIVE]


def run_auction(
    conn: sqlite3.Connection,
    trade_date: str,
    pattern: AuctionPattern,
    now: datetime,
) -> dict[str, Any]:
    before = load_active_signals(conn, trade_date)
    after = apply_auction(before, pattern, now)
    invalidated = [signal for signal in after if signal.status is SignalStatus.INVALIDATED]
    for signal in invalidated:
        save_signal(conn, signal)
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status=SignalStatus.ACTIVE.value,
            to_status=SignalStatus.INVALIDATED.value,
            reason='auction_weak',
            created_at=now,
        ))
    conn.commit()
    return {'pattern': pattern.value, 'invalidated': [signal.id for signal in invalidated]}


def run_watch(
    conn: sqlite3.Connection,
    price: float,
    second_cycle: bool,
    now: datetime,
    volume_ratio: float | None = None,
    trade_date: str | None = None,
    broken_signal_ids: Iterable[str] | None = None,
    break_context: Mapping[str, Mapping[str, Any]] | None = None,
    retest_volume_min: float = 1.2,
    prices: Mapping[str, float] | None = None,
    context_prices: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    touched: list[str] = []
    invalidated: list[str] = []
    broken_ids = set(broken_signal_ids or ())
    break_decisions: list[dict[str, str]] = []
    correction_notices: list[str] = []
    if context_prices is not None:
        for mark in load_context_marks(conn, now.date().isoformat()):
            price = (context_prices.get(mark.context_type.value) or {}).get(mark.code)
            if price is not None and mark.level_low <= price <= mark.level_high:
                touch_context_mark(conn, mark.date.isoformat(), mark.context_type, mark.code, now)
        conn.commit()
    for signal in load_active_signals(conn, trade_date):
        signal_price = (prices or {}).get(signal.symbol) if prices is not None else price
        if signal_price is None:
            continue
        if signal.id in broken_ids:
            updated, journal = invalidate_premise(signal, now)
            save_signal(conn, updated)
            save_journal(conn, journal)
            invalidated.append(signal.id)
            continue
        decision: tuple[BreakQualifier, str] | None = None
        if signal.tag is SignalTag.STOP:
            context = (break_context or {}).get(signal.symbol)
            if context is None:
                context = context_from_marks(conn, signal.symbol, now)
            decision = stop_break_decision(context)
        updated, journal, reason = process_touch(
            signal,
            signal_price,
            now=now,
            volume_ratio=volume_ratio,
            retest_volume_min=retest_volume_min,
        )
        if updated is not None and journal is not None:
            if decision is not None:
                updated = updated.model_copy(update={'break_qualifier': decision[0].value})
                qualified_reason = f"{reason}:{decision[0].value}:{decision[1]}"
                updated = updated.model_copy(update={'status_reason': qualified_reason})
                journal = journal.model_copy(update={'reason': qualified_reason})
                break_decisions.append({
                    'signal_id': signal.id,
                    'qualifier': decision[0].value,
                    'reason': decision[1],
                })
            save_signal(conn, updated)
            save_journal(conn, journal)
            if signal.tag is SignalTag.STOP and decision is not None:
                save_journal(conn, journal.model_copy(update={
                    'record_type': JournalRecordType.QUALIFIER,
                    'reason': decision[0].value,
                }))
            touched.append(signal.id)
            if reason == 'retest_pass':
                from .settle import correction_notice

                correction_notices.append(correction_notice(updated, 'retest_pass'))
    conn.commit()
    return {
        'touched': touched,
        'premise_broken': invalidated,
        'break_decisions': break_decisions,
        'correction_notices': correction_notices,
    }


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
    volume_ratios: Mapping[str, float] | None = None,
    break_contexts: Mapping[str, Mapping[str, Any]] | None = None,
    retest_window_days: int = 3,
    retest_volume_min: float = 1.2,
    holidays: set | None = None,
    market_tier: str = 'NORMAL',
    index_trend: str = 'UP',
) -> dict[str, Any]:
    all_signals = load_signals(conn)
    original_signals = {signal.id: signal for signal in all_signals}
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
        and signal.symbol in stop_zones
    ]
    for signal in rolled:
        save_signal(conn, signal)
        original = original_signals.get(signal.id)
        if original and (original.zone_low, original.zone_high) != (signal.zone_low, signal.zone_high):
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
    from .settle import correction_notice, stop_break_qualifier

    for stop in [
        signal for signal in all_signals
        if signal.tag.value == 'stop'
        and signal.status is SignalStatus.TRIGGERED
        and signal.created_at.date() == now.date()
    ]:
        if stop.break_qualifier:
            from ..core.volume import BreakQualifier as QualifierEnum

            qualifier = QualifierEnum(stop.break_qualifier)
        elif stop.symbol in (break_contexts or {}):
            context = (break_contexts or {})[stop.symbol]
            from ..jobs.watch import stop_break_decision

            qualifier, _ = stop_break_decision(context)
        else:
            qualifier = stop_break_qualifier(stop)
        close_price = (close_prices or {}).get(stop.symbol)
        if qualifier is not None and qualifier.value == 'LIMIT_EXEMPT':
            continue
        if (
            market_tier == 'LOW' and index_trend == 'DOWN'
            and qualifier.value in {'DELAY_OBSERVE', 'DIVERGENCE'}
        ):
            from ..core.volume import BreakQualifier as QualifierEnum

            qualifier = QualifierEnum.EXECUTE_NOW
        if qualifier.value in {'DELAY_OBSERVE', 'DIVERGENCE'} and close_price is not None:
            if close_price >= stop.zone_low:
                correction_notices.append(correction_notice(stop, 'same_day_close'))
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
    for retest in [
        signal for signal in load_signals(conn)
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
                correction_notices.append(correction_notice(updated, 'retest_pass'))
                settled = True
        elif price is not None and retest.zone_low <= price < retest.zone_high:
            updated, journal = settle_retest(retest, False, price, now)
            save_signal(conn, updated)
            save_journal(conn, journal)
            settled_retests.append(retest.id)
            settled = True
        if settled:
            continue
        from .settle import expire_retest

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


def run_restore(
    conn: sqlite3.Connection,
    close_price: float,
) -> dict[str, Any]:
    reminders = restore_reminders(load_signals(conn), close_price)
    from .settle import correction_notice

    return {
        'reminders': [signal.id for signal in reminders],
        'correction_notices': [
            correction_notice(signal, 'same_day_close') for signal in reminders
        ],
    }


def run_evening(conn: sqlite3.Connection, input_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding='utf-8'))
    trade_date = str(payload['trade_date'])
    created_at = datetime.fromisoformat(str(payload.get('created_at') or trade_date + 'T15:30:00+08:00'))
    strategies_path = payload.get('strategies_path') or Path(__file__).parents[1] / 'config' / 'strategies.yaml'

    from ..core.generator import load_strategies

    strategies = {strategy.id: strategy for strategy in load_strategies(strategies_path)}
    existing_signals = load_signals(conn)
    paused_symbols = protection_pause_symbols(conn)
    plans = list(payload.get('plans') or [])
    assignments = list(payload.get('assignments') or [])
    holdings = [
        row for row in (payload.get('holdings') or [])
        if str(row.get('symbol') or '') not in paused_symbols
    ]
    holding_rows = build_holding_rows(holdings, created_at)
    if assignments:
        plans.extend(build_pool_plans(strategies.values(), assignments))
    plans = [
        plan for plan in plans
        if str(plan.get('symbol') or '') not in paused_symbols
    ]
    rows = build_evening_signals(
        strategies,
        plans,
        str(payload.get('market_tier') or 'NORMAL'),
        created_at,
        existing_signals,
    )
    rows.extend(holding_rows)
    context_marks = build_context_marks(
        payload.get('index_context') or [],
        payload.get('sector_context') or [],
        date.fromisoformat(trade_date),
        created_at,
    )
    conn.execute(
        "UPDATE context_marks SET status='INACTIVE' WHERE date < ? AND status='ACTIVE'",
        (trade_date,),
    )
    save_context_marks(conn, context_marks)
    save_event_calendar(conn, build_event_calendar(
        strategies, plans, date.fromisoformat(trade_date), created_at,
    ))
    quality_rows = [
        dict(row)
        for row in conn.execute('SELECT id, strategy_id, status FROM signals').fetchall()
    ]
    ev_negative = {
        strategy_id
        for strategy_id, quality in signal_quality(quality_rows).items()
        if quality['ev_negative']
    }
    rows = [
        signal.model_copy(update={'premise': {**signal.premise, 'ev_negative': True}})
        if signal.strategy_id in ev_negative
        else signal
        for signal in rows
    ]
    save_signals(conn, rows)
    conn.commit()
    return {
        'trade_date': trade_date,
        'signal_ids': [signal.id for signal in rows],
    }
