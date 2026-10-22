from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.auction import AuctionPattern
from ..core.lifecycle import JournalEntry
from ..db.session import save_journal, save_signal, save_signals
from ..models.signal import Signal, SignalStatus
from .auction_call import apply_auction
from .evening import build_context_rows, build_evening_signals
from .restore_check import restore_reminders
from .settle import create_retest_confirm, expire_intraday
from .watch import process_touch


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
) -> dict[str, Any]:
    touched: list[str] = []
    waiting: list[str] = []
    for signal in load_active_signals(conn, trade_date):
        updated, journal, reason = process_touch(
            signal, price, second_cycle, volume_ratio, now
        )
        if reason == 'waiting_second_cycle':
            waiting.append(signal.id)
        elif updated is not None and journal is not None:
            save_signal(conn, updated)
            save_journal(conn, journal)
            touched.append(signal.id)
    conn.commit()
    return {'touched': touched, 'waiting': waiting}


def run_settle(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    all_signals = load_signals(conn)
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

    existing_ids = {signal.id for signal in load_signals(conn)}
    created_retests: list[str] = []
    for stop in [
        signal for signal in all_signals
        if signal.tag.value == 'stop'
        and signal.status is SignalStatus.TRIGGERED
        and signal.created_at.date() == now.date()
    ]:
        retest = create_retest_confirm(
            stop, (stop.zone_low, stop.zone_high), now
        )
        if retest.id in existing_ids:
            continue
        save_signal(conn, retest)
        existing_ids.add(retest.id)
        created_retests.append(retest.id)
    conn.commit()
    return {'expired': [signal.id for signal in expired], 'retest_created': created_retests}


def run_restore(
    conn: sqlite3.Connection,
    close_price: float,
) -> dict[str, Any]:
    reminders = restore_reminders(load_signals(conn), close_price)
    return {'reminders': [signal.id for signal in reminders]}


def run_evening(conn: sqlite3.Connection, input_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding='utf-8'))
    trade_date = str(payload['trade_date'])
    created_at = datetime.fromisoformat(str(payload.get('created_at') or trade_date + 'T15:30:00+08:00'))
    strategies_path = payload.get('strategies_path') or Path(__file__).parents[1] / 'config' / 'strategies.yaml'

    from ..core.generator import load_strategies

    strategies = {strategy.id: strategy for strategy in load_strategies(strategies_path)}
    rows = build_evening_signals(
        strategies,
        payload.get('plans') or [],
        str(payload.get('market_tier') or 'NORMAL'),
        created_at,
    )
    rows.extend(build_context_rows(
        payload.get('index_context') or [],
        payload.get('sector_context') or [],
        created_at,
    ))
    save_signals(conn, rows)
    conn.commit()
    return {
        'trade_date': trade_date,
        'signal_ids': [signal.id for signal in rows],
    }
