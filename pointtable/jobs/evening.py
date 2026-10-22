from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..core.generator import (
    Strategy,
    apply_size_hints,
    generate_context_row,
    generate_entry,
    premise_ok,
)
from ..models.signal import Signal, SignalTrend
from ..notify.render import render_report


def market_tier(amount_yi: float, low_max: float = 9000, high_min: float = 13000) -> str:
    if amount_yi < 0:
        raise ValueError('amount_yi must not be negative')
    if amount_yi < low_max:
        return 'LOW'
    if amount_yi >= high_min:
        return 'HIGH'
    return 'NORMAL'


def zero_plan_day(amount_yi: float, index_trend: str) -> bool:
    return market_tier(amount_yi) == 'LOW' and str(index_trend).upper() == 'DOWN'


def build_context_rows(
    index_rows: list[Mapping[str, Any]],
    sector_rows: list[Mapping[str, Any]],
    created_at: datetime,
) -> list[Signal]:
    context: list[Signal] = []
    for row in [*index_rows, *sector_rows]:
        symbol = str(row.get('symbol') or '')
        zone = row.get('zone')
        if symbol and zone and float(zone[0]) < float(zone[1]):
            context.append(generate_context_row(symbol, (float(zone[0]), float(zone[1])), created_at))
    return context


def build_evening_signals(
    strategies: Mapping[str, Strategy],
    pool_plans: list[Mapping[str, Any]],
    market_tier_name: str,
    created_at: datetime,
) -> list[Signal]:
    rows: list[Signal] = []
    for plan in pool_plans:
        strategy = strategies.get(str(plan.get('strategy_id') or ''))
        if strategy is None or not premise_ok(plan.get('premise_facts') or {}):
            continue
        rows.extend(generate_entry(
            strategy,
            str(plan['symbol']),
            (float(plan['entry_low']), float(plan['entry_high'])),
            (float(plan['stop_low']), float(plan['stop_high'])),
            (float(plan['profit_low']), float(plan['profit_high'])),
            created_at,
        ))
    return apply_size_hints(rows, market_tier_name)


def evening_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    receipt_coverage: float | None = None,
) -> str:
    context = dict(market)
    context['receipt_coverage'] = receipt_coverage
    context.setdefault('missed_stop_hit', any(position.get('missed_stop_date') for position in positions))
    context.setdefault('zero_plan_day', zero_plan_day(float(market.get('amount_yi') or 0), str(market.get('index_trend') or '')))
    return render_report(report_date, context, positions)
