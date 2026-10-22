from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


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
    from ..data import akshare

    symbols = set(_pool_symbols(conn, trade_date))
    rows = source_rows if source_rows is not None else akshare.stock_spot_rows()
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
