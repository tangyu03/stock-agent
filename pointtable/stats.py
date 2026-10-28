from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta
from .db.session import load_zone_snapshot, signal_from_row
from .jobs.restore_check import find_unpaired_t_buy


def signal_reachability(
    rows: Iterable[Mapping[str, Any]],
    market_rows: Iterable[Mapping[str, Any]] = (),
    *,
    tail_approx_tolerance: float = 0.003,
) -> dict[str, dict[str, Any]]:
    market_by_date_symbol = {
        (str(row.get('trade_date')), str(row['symbol'])): row
        for row in market_rows
        if row.get('symbol') is not None and row.get('trade_date') is not None
    }
    market_by_symbol = {
        str(row['symbol']): row
        for row in market_rows
        if row.get('symbol') is not None
    }

    def market_for_row(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return market_by_date_symbol.get(
            (str(row.get('created_at') or '')[:10], str(row.get('symbol'))),
            market_by_symbol.get(str(row.get('symbol'))),
        )

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get('strategy_id'))].append(row)
    report: dict[str, dict[str, Any]] = {}
    for strategy_id, items in grouped.items():
        total = len(items)
        audited_items = [
            row for row in items
            if (market := market_for_row(row)) is not None
            and not market.get('two_source_anomaly')
            and not row.get('audit_excluded')
        ]
        reachable = 0
        daily_reachable = 0
        unreachable = 0
        for row in items:
            row_date = str(row.get('created_at') or '')[:10]
            market = market_for_row(row)
            if market is None or market.get('two_source_anomaly') or row.get('audit_excluded'):
                continue
            zone_low = float(row.get('zone_low') or 0)
            zone_high = float(row.get('zone_high') or 0)
            role = str(row.get('role') or row.get('tag') or '')
            is_reachable = False
            is_daily_reachable = False
            if role == 'entry':
                low = float(market.get('low') or market.get('est_close') or 0)
                daily_ok = low <= zone_high
                mode = str(row.get('entry_mode') or 'tail_approx')
                if mode == 'tail_approx':
                    close = float(market.get('est_close') or market.get('close') or 0)
                    is_reachable = zone_low - tail_approx_tolerance <= close <= zone_high + tail_approx_tolerance
                else:
                    is_reachable = daily_ok
                is_daily_reachable = daily_ok
            elif role == 'stop':
                is_reachable = float(market.get('low') or 0) <= zone_low
                is_daily_reachable = is_reachable
            elif role == 'profit':
                is_reachable = float(market.get('high') or 0) >= zone_low
                is_daily_reachable = is_reachable
            elif role == 'retest_confirm':
                is_reachable = float(market.get('high') or 0) >= zone_low
                is_daily_reachable = is_reachable
            reachable += int(is_reachable)
            daily_reachable += int(is_daily_reachable)
            if str(row.get('status')) == 'TRIGGERED' and not is_reachable:
                unreachable += 1
        report[strategy_id] = {
            'total': total,
            'audited': len(audited_items),
            'audit_excluded': total - len(audited_items),
            'reachable': reachable,
            'touch_rate': reachable / len(audited_items) if audited_items else 0.0,
            'daily_reachable': daily_reachable,
            'daily_touch_rate': daily_reachable / len(audited_items) if audited_items else 0.0,
            'unreachable': unreachable,
            'unreachable_rate': unreachable / len(audited_items) if audited_items else 0.0,
        }
    return report


def signal_quality(
    rows: Iterable[Mapping[str, Any]],
    market_rows: Iterable[Mapping[str, Any]] = (),
    *,
    tail_approx_tolerance: float = 0.003,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row['strategy_id'])].append(row)
    report: dict[str, dict[str, Any]] = {}
    for strategy_id, items in grouped.items():
        total = len(items)
        market_by_date_symbol = {
            (str(row.get('trade_date')), str(row['symbol'])): row
            for row in market_rows
            if row.get('symbol') is not None and row.get('trade_date') is not None
        }
        market_by_symbol = {
            str(row['symbol']): row
            for row in market_rows
            if row.get('symbol') is not None
        }
        triggered = []
        for row in items:
            if str(row.get('status')) != 'TRIGGERED':
                continue
            row_date = str(row.get('created_at') or '')[:10]
            market = market_by_date_symbol.get((row_date, str(row.get('symbol'))))
            if market is None:
                market = market_by_symbol.get(str(row.get('symbol')))
            if row.get('audit_excluded') or (market or {}).get('two_source_anomaly'):
                continue
            triggered.append(row)
        pnl_values = [float(row.get('hypothetical_pnl_pct') or 0) for row in triggered]
        expectancy = sum(pnl_values) / len(pnl_values) if pnl_values else None
        rolling_values = pnl_values[-50:]
        rolling_expectancy = sum(rolling_values) / len(rolling_values) if rolling_values else None
        report[strategy_id] = {
            'total': total,
            'triggered': len(triggered),
            'touch_rate': len(triggered) / total if total else 0.0,
            'mechanical_expectancy': expectancy,
            'rolling_expectancy': rolling_expectancy,
            'ev_negative': rolling_expectancy is not None and len(triggered) >= 30 and rolling_expectancy <= 0,
            'reachability': signal_reachability(
                items,
                market_rows,
                tail_approx_tolerance=tail_approx_tolerance,
            ).get(strategy_id),
        }
    return dict(report)


def stop_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    stop_rows = [row for row in rows if str(row.get('tag')) == 'stop' and str(row.get('status')) == 'TRIGGERED']
    sell_fly = sum(bool(row.get('sell_fly')) for row in stop_rows)
    loss_avoided = sum(bool(row.get('loss_avoided')) for row in stop_rows)
    return {
        'sample': len(stop_rows),
        'sell_fly': sell_fly,
        'loss_avoided': loss_avoided,
        'sell_fly_rate': sell_fly / len(stop_rows) if stop_rows else None,
        'loss_avoided_rate': loss_avoided / len(stop_rows) if stop_rows else None,
    }


def correction_hit_rate(rows: Iterable[Mapping[str, Any]]) -> float | None:
    items = [row for row in rows if row.get('correction_hit') is not None]
    if not items:
        return None
    return sum(bool(row.get('correction_hit')) for row in items) / len(items)


def pool_quality(
    pool_rows: Iterable[Mapping[str, Any]],
    performance_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    returns_by_symbol = {
        str(row['symbol']): float(row['five_day_return'])
        for row in performance_rows
        if row.get('symbol') is not None and row.get('five_day_return') is not None
    }
    symbols = [str(row['symbol']) for row in pool_rows if row.get('symbol')]
    measured = [returns_by_symbol[symbol] for symbol in symbols if symbol in returns_by_symbol]
    trends = [str(row.get('trend') or 'unknown').upper() for row in pool_rows]
    return {
        'total': len(symbols),
        'measured': len(measured),
        'five_day_avg_return': sum(measured) / len(measured) if measured else None,
        'trend_distribution': {
            trend: trends.count(trend)
            for trend in ('UP', 'DOWN', 'UNKNOWN')
            if trends.count(trend)
        },
    }


def behavior(rows: Iterable[Mapping[str, Any]], t_rows: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    items = list(rows)
    stops = [row for row in items if str(row.get('tag')) == 'stop' and str(row.get('status')) == 'TRIGGERED']
    t_values = [float(row['diff_pct']) for row in t_rows if row.get('diff_pct') is not None]
    return {
        't_added_avg_pct': sum(t_values) / len(t_values) if t_values else None,
        'restore_violations': sum(bool(row.get('restore_violation')) for row in items),
    }
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
    journal_rows = conn.execute(
        "SELECT j.signal_id, j.created_at FROM journal j JOIN signals s ON s.id=j.signal_id "
        "WHERE s.tag='stop' AND j.to_status='TRIGGERED' ORDER BY j.created_at, j.signal_id"
    ).fetchall()
    correction_rows: list[dict[str, Any]] = []
    stop_tracking: list[dict[str, Any]] = []
    for journal in journal_rows:
        signal_id = str(journal['signal_id'])
        trigger_date = str(journal['created_at'])[:10]
        signal_row = conn.execute('SELECT * FROM signals WHERE id=?', (signal_id,)).fetchone()
        trigger_time = str(signal_row['closed_at']) if signal_row and signal_row['closed_at'] else str(journal['created_at'])
        zone = load_zone_snapshot(conn, signal_id, trigger_time)
        rows = _kline_rows(
            str(signal_row['symbol']) if signal_row else '',
            (date.fromisoformat(trigger_date) + timedelta(days=30)).isoformat(),
            stock_klines,
        )
        future_rows = [row for row in rows if (_row_date(row) or date.min) > date.fromisoformat(trigger_date)]
        if len(future_rows) < window:
            correction_rows.append({'signal_id': signal_id, 'correction_hit': None})
            continue
        future_close = _numeric(future_rows[window - 1], 'close', '收盘')
        zone_high = zone[1] if zone else None
        correction_rows.append({
            'signal_id': signal_id,
            'correction_hit': future_close is not None and zone_high is not None and future_close >= zone_high,
        })
        if zone is not None:
            tracked_closes = [
                close for close in (_numeric(row, 'close') for row in future_rows[:window])
                if close is not None
            ]
            recovered = any(close >= zone[1] for close in tracked_closes)
            stop_tracking.append({
                'signal_id': signal_id,
                'zone_low': zone[0],
                'zone_high': zone[1],
                'trigger_time': trigger_time,
                'sell_fly': recovered,
                'loss_avoided': not recovered,
            })

    t_signals = [signal_from_row(row) for row in conn.execute("SELECT * FROM signals WHERE tag='t'").fetchall()]
    unpaired, violations = find_unpaired_t_buy(t_signals, trade_date=trade_date)
    return {
        'performance': performance,
        'pool_quality': pool_quality(pool_rows, performance),
        'correction_hit_rate': correction_hit_rate(correction_rows),
        'stop_tracking': stop_tracking,
        'restore': {
            'unpaired_t_buy': [item.__dict__ for item in unpaired],
            'violations': violations,
        },
    }
