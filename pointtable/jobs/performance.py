from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from ..stats import correction_hit_rate, pool_quality
from .restore_check import find_unpaired_t_buy
from .routines import signal_from_row


def _numeric(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == '':
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _row_date(row: Mapping[str, Any]) -> date | None:
    raw = str(row.get('trade_date') or row.get('日期') or row.get('date') or '')
    raw = raw[:10].replace('/', '-')
    if len(raw) == 8:
        raw = f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def five_day_performance(
    symbol: str,
    trade_date: str,
    rows: Sequence[Mapping[str, Any]],
    window: int = 5,
) -> dict[str, Any] | None:
    base_date = date.fromisoformat(trade_date)
    dated_rows = [(_row_date(row), row) for row in rows]
    dated_rows = [(row_date, row) for row_date, row in dated_rows if row_date is not None]
    base_index = max(
        (index for index, (row_date, _) in enumerate(dated_rows) if row_date <= base_date),
        default=None,
    )
    if base_index is None:
        return None
    base_row = dated_rows[base_index][1]
    future_index = base_index + window
    if future_index >= len(dated_rows):
        return None
    base_close = _numeric(base_row, 'close', '收盘')
    future_close = _numeric(dated_rows[future_index][1], 'close', '收盘')
    if base_close is None or future_close is None or base_close == 0:
        return None
    return {
        'symbol': symbol,
        'base_date': dated_rows[base_index][0].isoformat(),
        'base_close': base_close,
        'future_date': dated_rows[future_index][0].isoformat(),
        'future_close': future_close,
        'five_day_return': future_close / base_close - 1,
    }


def _kline_rows(
    symbol: str,
    end_date: str,
    stock_klines: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> list[Mapping[str, Any]]:
    if stock_klines is not None:
        return list(stock_klines.get(symbol, ()))
    from ..data import akshare

    return list(akshare.stock_kline(symbol, trade_date=end_date, limit=120))


def pool_performance_report(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    window: int = 5,
    stock_klines: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    pool_rows = [
        dict(row) for row in conn.execute(
            'SELECT symbol, theme_label FROM pool WHERE date=? ORDER BY symbol',
            (trade_date,),
        ).fetchall()
    ]
    symbols = [str(row['symbol']) for row in pool_rows]
    horizon = (date.fromisoformat(trade_date) + timedelta(days=30)).isoformat()
    performance: list[dict[str, Any]] = []
    for symbol in symbols:
        item = five_day_performance(
            symbol,
            trade_date,
            _kline_rows(symbol, horizon, stock_klines),
            window,
        )
        if item is not None:
            performance.append(item)

    signal_rows = conn.execute("SELECT * FROM signals WHERE tag='stop'").fetchall()
    stop_zones = {str(row['id']): float(row['zone_high']) for row in signal_rows}
    journal_rows = conn.execute(
        "SELECT j.signal_id, j.created_at FROM journal j JOIN signals s ON s.id=j.signal_id "
        "WHERE s.tag='stop' AND j.to_status='TRIGGERED' ORDER BY j.created_at, j.signal_id"
    ).fetchall()
    correction_rows: list[dict[str, Any]] = []
    for journal in journal_rows:
        signal_id = str(journal['signal_id'])
        trigger_date = str(journal['created_at'])[:10]
        rows = _kline_rows(
            str(conn.execute('SELECT symbol FROM signals WHERE id=?', (signal_id,)).fetchone()['symbol']),
            (date.fromisoformat(trigger_date) + timedelta(days=30)).isoformat(),
            stock_klines,
        )
        future_rows = [row for row in rows if (_row_date(row) or date.min) > date.fromisoformat(trigger_date)]
        if len(future_rows) < window:
            correction_rows.append({'signal_id': signal_id, 'correction_hit': None})
            continue
        future_close = _numeric(future_rows[window - 1], 'close', '收盘')
        zone_high = stop_zones.get(signal_id)
        correction_rows.append({
            'signal_id': signal_id,
            'correction_hit': future_close is not None and zone_high is not None and future_close >= zone_high,
        })

    t_signals = [signal_from_row(row) for row in conn.execute("SELECT * FROM signals WHERE tag='t'").fetchall()]
    receipt_rows = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts').fetchall()]
    unpaired, no_receipt, violations = find_unpaired_t_buy(t_signals, receipt_rows, trade_date)
    return {
        'performance': performance,
        'pool_quality': pool_quality(pool_rows, performance),
        'correction_hit_rate': correction_hit_rate(correction_rows),
        'restore': {
            'unpaired_t_buy': [item.__dict__ for item in unpaired],
            'no_receipt': no_receipt,
            'violations': violations,
        },
    }
