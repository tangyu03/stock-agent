from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any

from ..core.generator import (
    Strategy,
    apply_size_hints,
    generate_holding_rows,
    generate_entry,
    premise_ok,
)
from ..models.context_mark import ContextMark, ContextType
from ..models.event_calendar import EventCalendar
from ..models.signal import Signal, SignalTag, SignalTrend, SignalValidPolicy
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


def build_context_marks(
    index_rows: list[Mapping[str, Any]],
    sector_rows: list[Mapping[str, Any]],
    trade_date: date,
    created_at: datetime,
) -> list[ContextMark]:
    marks: list[ContextMark] = []
    for context_type, rows in (
        (ContextType.INDEX, index_rows),
        (ContextType.SECTOR, sector_rows),
    ):
        for row in rows:
            symbol = str(row.get('symbol') or '')
            zone = row.get('zone')
            if not symbol or not zone or float(zone[0]) >= float(zone[1]):
                continue
            marks.append(ContextMark(
                date=trade_date,
                context_type=context_type,
                code=symbol,
                level_low=float(zone[0]),
                level_high=float(zone[1]),
                label=str(row.get('label') or ''),
                triggered_at=None,
                created_at=created_at,
            ))
    return marks


def build_evening_signals(
    strategies: Mapping[str, Strategy],
    pool_plans: list[Mapping[str, Any]],
    market_tier_name: str,
    created_at: datetime,
    existing_signals: list[Signal] | None = None,
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
            parent_id=plan.get('parent_id'),
            existing_signals=existing_signals,
        ))
    return apply_size_hints(rows, market_tier_name)


def build_event_calendar(
    strategies: Mapping[str, Strategy],
    pool_plans: list[Mapping[str, Any]],
    trade_date: date,
    created_at: datetime,
) -> list[EventCalendar]:
    events: dict[str, EventCalendar] = {}
    for plan in pool_plans:
        strategy = strategies.get(str(plan.get('strategy_id') or ''))
        symbol = str(plan.get('symbol') or '')
        if strategy is None or not symbol or str(strategy.y.get('when') or '') != 'tail_snapshot':
            continue
        label = str(strategy.y.get('label') or f'{strategy.id}:尾盘触发')
        events[symbol] = EventCalendar(
            date=trade_date,
            code=symbol,
            label=label,
            due_time=time(14, 30),
            payload={'strategy_id': strategy.id},
            created_at=created_at,
        )
    return list(events.values())


def build_holding_rows(
    holdings: list[Mapping[str, Any]],
    created_at: datetime,
) -> list[Signal]:
    rows: list[Signal] = []
    for holding in holdings:
        symbol = str(holding.get('symbol') or '')
        parent_id = holding.get('parent_id')
        stop_zone = holding.get('stop_zone')
        support_zone = holding.get('support_zone')
        resistance_zone = holding.get('resistance_zone')
        if not symbol or not parent_id or not stop_zone or not support_zone or not resistance_zone:
            raise ValueError('holding rows require symbol, parent_id and three zones')
        trend = SignalTrend.UP if str(holding.get('trend') or 'UP').upper() == 'UP' else SignalTrend.DOWN
        generated = generate_holding_rows(
            symbol,
            trend,
            (float(stop_zone[0]), float(stop_zone[1])),
            (float(support_zone[0]), float(support_zone[1])),
            (float(resistance_zone[0]), float(resistance_zone[1])),
            str(parent_id),
            created_at,
        )
        if holding.get('volatility_exceeded'):
            generated = [row for row in generated if row.tag is not SignalTag.T]
        rows.extend(generated)
    return rows


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
