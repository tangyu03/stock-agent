from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..core.ma import ma_tomorrow


def _zone(center: float, width_pct: float) -> tuple[float, float]:
    if center <= 0:
        raise ValueError('zone center must be positive')
    half = center * width_pct
    return round(center - half, 6), round(center + half, 6)


def _break_zone(level: float, width_pct: float) -> tuple[float, float]:
    if level <= 0:
        raise ValueError('break level must be positive')
    return round(level * (1 - width_pct), 6), round(level, 6)


def _strategy_value(strategy: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(strategy, Mapping):
        return strategy[key]
    return getattr(strategy, key)


def _strategy_part(strategy: Mapping[str, Any] | Any, key: str) -> dict[str, Any]:
    return dict(_strategy_value(strategy, key))


def build_pool_plan(
    strategy: Mapping[str, Any],
    symbol: str,
    closes: Sequence[float],
    est_close: float,
    *,
    support: float | None = None,
    resistance: float | None = None,
    breakout_price: float | None = None,
    parent_id: str | None = None,
    premise_facts: Mapping[str, bool] | None = None,
    zone_width_pct: float = 0.01,
) -> dict[str, Any]:
    if not symbol:
        raise ValueError('symbol must not be empty')
    if est_close <= 0:
        raise ValueError('est_close must be positive')
    if zone_width_pct <= 0 or zone_width_pct >= 0.5:
        raise ValueError('zone_width_pct must be between 0 and 0.5')
    if any(close <= 0 for close in closes):
        raise ValueError('closes must be positive')

    y = _strategy_part(strategy, 'y')
    z = _strategy_part(strategy, 'z')
    w = _strategy_part(strategy, 'w')
    y_type = str(y.get('type'))
    if y_type == 'ma_tomorrow':
        line = ma_tomorrow(closes, int(y['n']), est_close)
        entry_zone = _zone(line, zone_width_pct)
        stop_zone = _break_zone(line, zone_width_pct)
    elif y_type == 'pullback':
        level = est_close * float(y.get('ratio', 0.98))
        entry_zone = _zone(level, zone_width_pct)
        stop_level = breakout_price or est_close
        stop_zone = _break_zone(stop_level, zone_width_pct)
    elif y_type == 'intraday_support':
        if support is None:
            return {}
        entry_zone = _zone(support, zone_width_pct)
        n = int(z.get('n', 5))
        if len(closes) < n - 1:
            return {}
        stop_level = ma_tomorrow(closes, n, est_close)
        stop_zone = _break_zone(stop_level, zone_width_pct)
    else:
        raise ValueError(f'unsupported strategy y type: {y_type}')

    if str(w.get('type')) in {'pressure_zone', 'next_resistance'} and resistance:
        profit_zone = (round(resistance * 0.995, 6), round(resistance * 1.005, 6))
    else:
        profit_zone = (round(entry_zone[1] * 1.02, 6), round(entry_zone[1] * 1.04, 6))

    if entry_zone[1] <= stop_zone[1]:
        return {}
    return {
        'symbol': symbol,
        'strategy_id': str(_strategy_value(strategy, 'id')),
        'entry_low': entry_zone[0],
        'entry_high': entry_zone[1],
        'stop_low': stop_zone[0],
        'stop_high': stop_zone[1],
        'profit_low': profit_zone[0],
        'profit_high': profit_zone[1],
        'parent_id': parent_id,
        'premise_facts': dict(premise_facts or {}),
    }


def build_pool_plans(
    strategies: Iterable[Mapping[str, Any] | Any],
    assignments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(_strategy_value(strategy, 'id')): strategy for strategy in strategies}
    plans: list[dict[str, Any]] = []
    for assignment in assignments:
        strategy = by_id.get(str(assignment.get('strategy_id') or ''))
        if strategy is None:
            continue
        plan = build_pool_plan(
            strategy,
            str(assignment.get('symbol') or ''),
            assignment.get('closes') or [],
            float(assignment.get('est_close') or 0),
            support=assignment.get('support'),
            resistance=assignment.get('resistance'),
            breakout_price=assignment.get('breakout_price'),
            parent_id=assignment.get('parent_id'),
            premise_facts=assignment.get('premise_facts'),
            zone_width_pct=float(assignment.get('zone_width_pct') or 0.01),
        )
        if plan:
            plans.append(plan)
    return plans
