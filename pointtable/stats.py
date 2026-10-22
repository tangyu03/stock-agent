from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


def signal_quality(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row['strategy_id'])].append(row)
    report: dict[str, dict[str, Any]] = {}
    for strategy_id, items in grouped.items():
        total = len(items)
        triggered = [row for row in items if str(row.get('status')) == 'TRIGGERED']
        confirmed = [row for row in triggered if bool(row.get('confirmed'))]
        pnl_values = [float(row.get('hypothetical_pnl_pct') or 0) for row in confirmed]
        expectancy = sum(pnl_values) / len(pnl_values) if pnl_values else None
        rolling_values = pnl_values[-50:]
        rolling_expectancy = sum(rolling_values) / len(rolling_values) if rolling_values else None
        report[strategy_id] = {
            'total': total,
            'triggered': len(triggered),
            'confirmed': len(confirmed),
            'touch_rate': len(triggered) / total if total else 0.0,
            'confirmed_expectancy': expectancy,
            'rolling_expectancy': rolling_expectancy,
            'ev_negative': rolling_expectancy is not None and len(confirmed) >= 30 and rolling_expectancy <= 0,
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
    receipts = [row for row in items if row.get('verdict')]
    executed = sum(bool(row.get('verdict') == 'executed') for row in receipts)
    coverage = len(receipts) / len(items) if receipts and items else None
    if coverage is not None and coverage < 0.5:
        return {
            'receipt_coverage': coverage,
            'insufficient_data': True,
            'discipline_rate': None,
            't_added_avg_pct': None,
            'restore_violations': None,
        }
    stops = [row for row in items if str(row.get('tag')) == 'stop' and str(row.get('status')) == 'TRIGGERED']
    executed_stops = sum(bool(row.get('verdict') == 'executed') for row in stops)
    t_values = [float(row['diff_pct']) for row in t_rows if row.get('diff_pct') is not None]
    return {
        'receipt_coverage': coverage,
        'insufficient_data': False,
        'discipline_rate': executed_stops / len(stops) if stops else None,
        't_added_avg_pct': sum(t_values) / len(t_values) if t_values else None,
        'restore_violations': sum(bool(row.get('restore_violation')) for row in items),
    }
