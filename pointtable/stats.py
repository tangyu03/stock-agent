from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


def signal_reachability(
    rows: Iterable[Mapping[str, Any]],
    market_rows: Iterable[Mapping[str, Any]] = (),
    *,
    tail_approx_tolerance: float = 0.003,
) -> dict[str, dict[str, Any]]:
    market_by_symbol = {
        str(row['symbol']): row
        for row in market_rows
        if row.get('symbol') is not None
    }
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get('strategy_id'))].append(row)
    report: dict[str, dict[str, Any]] = {}
    for strategy_id, items in grouped.items():
        total = len(items)
        reachable = 0
        daily_reachable = 0
        unreachable = 0
        for row in items:
            market = market_by_symbol.get(str(row.get('symbol')))
            if market is None:
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
            'reachable': reachable,
            'touch_rate': reachable / total if total else 0.0,
            'daily_reachable': daily_reachable,
            'daily_touch_rate': daily_reachable / total if total else 0.0,
            'unreachable': unreachable,
            'unreachable_rate': unreachable / total if total else 0.0,
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
        triggered = [row for row in items if str(row.get('status')) == 'TRIGGERED']
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
