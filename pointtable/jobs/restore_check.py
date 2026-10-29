from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..models.signal import Signal, SignalStatus, SignalTag
import sqlite3
from datetime import datetime
from typing import Any
from ..db.session import load_signals
from .settle import correction_notice


@dataclass(frozen=True)
class UnpairedT:
    signal_id: str
    symbol: str
    parent_id: str
    pair_id: str | None
    trade_date: str


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



def _signal_trade_date(signal: Signal) -> str:
    first_part = signal.id.split('-', 1)[0]
    if len(first_part) == 10 and first_part[4:5] == '-' and first_part[7:8] == '-':
        return first_part
    return signal.created_at.isoformat(timespec='seconds')[:10]


def find_unpaired_t_buy(
    signals: Iterable[Signal],
    trade_date: str | None = None,
    *,
    active_only: bool = False,
) -> tuple[list[UnpairedT], list[str]]:
    rows = list(signals)
    t_rows = [row for row in rows if row.tag is SignalTag.T]
    if trade_date is not None:
        t_rows = [
            row for row in t_rows
            if (row.valid_for.isoformat() if row.valid_for else _signal_trade_date(row)) == trade_date
        ]
    sells_by_pair = {
        row.pair_id or row.parent_id: row
        for row in t_rows
        if row.side.value == 'SELL'
    }
    unpaired: list[UnpairedT] = []
    violations: list[str] = []
    for signal in t_rows:
        if signal.side.value != 'BUY' or not signal.parent_id:
            continue
        # 已终态的历史 T 买不再可补卖，只在审计口径要求“仍待处理”时过滤
        if active_only and signal.status is not SignalStatus.ACTIVE:
            continue
        paired_sell = sells_by_pair.get(signal.pair_id or signal.parent_id)
        if paired_sell and paired_sell.symbol != signal.symbol:
            paired_sell = None
        if paired_sell is None:
            unpaired.append(UnpairedT(
                signal_id=signal.id,
                symbol=signal.symbol,
                parent_id=signal.parent_id,
                pair_id=signal.pair_id,
                trade_date=_signal_trade_date(signal),
            ))
            violations.append(signal.id)
    return unpaired, violations


def cascade_violations(signals: Iterable[Signal]) -> list[dict[str, str]]:
    '''§4 区间不变量4 审计：stop TRIGGERED 后同票仍存留未触发的 ACTIVE T_BUY。'''
    triggered_stop_symbols = {
        signal.symbol
        for signal in signals
        if signal.tag is SignalTag.STOP and signal.status is SignalStatus.TRIGGERED
    }
    rows: list[dict[str, str]] = []
    for signal in signals:
        if (
            signal.symbol in triggered_stop_symbols
            and signal.tag is SignalTag.T
            and signal.side is SignalSide.BUY
            and signal.status is SignalStatus.ACTIVE
        ):
            rows.append({'signal_id': signal.id, 'symbol': signal.symbol})
    return rows
def _pool_symbols(conn: sqlite3.Connection, trade_date: str) -> list[str]:
    rows = conn.execute(
        '''
        SELECT symbol FROM pool
        WHERE date = (SELECT MAX(date) FROM pool WHERE date <= ?)
        ORDER BY symbol
        ''',
        (trade_date,),
    ).fetchall()
    return [str(row['symbol']) for row in rows]


def save_tail_snapshot(
    conn: sqlite3.Connection,
    trade_date: str,
    symbol: str,
    price: float,
    captured_at: datetime,
    *,
    source: str = 'spot',
) -> None:
    conn.execute(
        '''
        INSERT INTO tail_snapshots (trade_date, symbol, captured_at, price, source)
        VALUES (?,?,?,?,?)
        ON CONFLICT(trade_date, symbol) DO UPDATE SET
            captured_at=excluded.captured_at,
            price=excluded.price,
            source=excluded.source
        ''',
        (trade_date, symbol, captured_at.isoformat(timespec='seconds'), float(price), source),
    )


def load_tail_prices(conn: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    rows = conn.execute(
        'SELECT symbol, price FROM tail_snapshots WHERE trade_date=? ORDER BY symbol',
        (trade_date,),
    ).fetchall()
    return {str(row['symbol']): float(row['price']) for row in rows}


def capture_tail_snapshots(
    conn: sqlite3.Connection,
    trade_date: str,
    now: datetime,
    *,
    source_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:

    symbols = set(_pool_symbols(conn, trade_date))
    pool_symbols = sorted(symbols)
    if source_rows is not None:
        rows = source_rows
    else:
        from ..data import akshare

        rows = akshare.stock_spot_quotes(pool_symbols)
    captured: list[str] = []
    for row in rows:
        symbol = str(row.get('symbol') or '')
        price = row.get('price')
        if symbol in symbols and price is not None:
            save_tail_snapshot(
                conn,
                trade_date,
                symbol,
                float(price),
                now,
                source=str(row.get('source') or 'spot'),
            )
            captured.append(symbol)
    conn.commit()
    return {'trade_date': trade_date, 'captured': sorted(captured)}


def run_restore(
    conn: sqlite3.Connection,
    close_price: float,
) -> dict[str, Any]:
    reminders = restore_reminders(load_signals(conn), close_price)
    
    return {
        'reminders': [signal.id for signal in reminders],
        'correction_notices': [
            correction_notice(signal, 'same_day_close') for signal in reminders
        ],
    }
