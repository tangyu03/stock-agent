from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

from .ma import ma_tomorrow
from ..models.signal import (
    Signal,
    SignalRole,
    SignalSide,
    SignalStatus,
    SignalTag,
    SignalTrend,
    SignalValidPolicy,
)


class PushPriority(str, Enum):
    STOP = 0
    RETEST_CONFIRM = 1
    PROFIT = 2
    T = 3
    ENTER = 4


class TSizeChoice(str, Enum):
    SAME = 'same'
    DOUBLE = 'double'
    HALF = 'half'


_PRIORITY = {
    SignalTag.STOP: PushPriority.STOP,
    SignalTag.RETEST_CONFIRM: PushPriority.RETEST_CONFIRM,
    SignalTag.PROFIT: PushPriority.PROFIT,
    SignalTag.T: PushPriority.T,
    SignalTag.ENTER: PushPriority.ENTER,
    SignalTag.INDEX_CONTEXT: PushPriority.ENTER,
    SignalTag.SECTOR_CONTEXT: PushPriority.ENTER,
    SignalTag.TIME_TRIGGER: PushPriority.ENTER,
}


@dataclass(frozen=True)
class Strategy:
    id: str
    side: str
    premise: str
    y: Mapping[str, Any]
    z: Mapping[str, Any]
    w: Mapping[str, Any]


def load_strategies(path: str | Path) -> list[Strategy]:
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    result = []
    for item in raw.get('strategies') or []:
        strategy = Strategy(
            id=str(item['id']),
            side=str(item['side']),
            premise=str(item['premise']),
            y=dict(item['y']),
            z=dict(item['z']),
            w=dict(item['w']),
        )
        validate_strategy(strategy)
        result.append(strategy)
    return result


def validate_strategy(strategy: Strategy) -> None:
    if not strategy.id or strategy.side != 'BUY':
        raise ValueError('strategy id must not be empty and side must be BUY')
    if not strategy.premise.strip():
        raise ValueError(f'strategy {strategy.id} requires non-empty premise')
    for key in ('y', 'z', 'w'):
        value = getattr(strategy, key)
        if not value or 'type' not in value:
            raise ValueError(f'strategy {strategy.id} requires non-empty {key}')


def premise_ok(facts: Mapping[str, bool]) -> bool:
    return all(facts.values())


def _signal(
    signal_id: str,
    strategy_id: str,
    symbol: str,
    side: str,
    tag: SignalTag,
    zone: tuple[float, float],
    trend: SignalTrend,
    created_at: datetime,
    parent_id: str | None = None,
    valid_policy: SignalValidPolicy = SignalValidPolicy.INTRADAY,
) -> Signal:
    return Signal(
        id=signal_id,
        symbol=symbol,
        strategy_id=strategy_id,
        side=side,
        tag=tag,
        role={
            SignalTag.ENTER: SignalRole.ENTRY,
            SignalTag.STOP: SignalRole.STOP,
            SignalTag.PROFIT: SignalRole.PROFIT,
            SignalTag.T: SignalRole.T,
            SignalTag.RETEST_CONFIRM: SignalRole.RETEST_CONFIRM,
        }[tag],
        zone_low=float(zone[0]),
        zone_high=float(zone[1]),
        trend=trend,
        valid_policy=valid_policy,
        created_at=created_at,
        parent_id=parent_id,
    )


def generate_entry(
    strategy: Strategy,
    symbol: str,
    entry_zone: tuple[float, float],
    created_at: datetime,
    trade_date: date | None = None,
    parent_id: str | None = None,
) -> list[Signal]:
    validate_strategy(strategy)
    trade_date = trade_date or created_at.date()
    enter_id = f'{trade_date.isoformat()}-{symbol}-{strategy.id}'
    trend = SignalTrend.UP
    trigger_window = (
        (time(14, 30), time(15, 0))
        if str(strategy.y.get('when') or '') == 'tail_snapshot'
        else None
    )
    enter = _signal(
        enter_id, strategy.id, symbol, 'BUY', SignalTag.ENTER,
        entry_zone, trend, created_at, parent_id,
        valid_policy=SignalValidPolicy.NEXT_DAY,
    )
    enter = enter.model_copy(update={'premise': {'premise': strategy.premise}})
    if trigger_window is not None:
        enter = enter.model_copy(update={
            'trigger_window_start': trigger_window[0],
            'trigger_window_end': trigger_window[1],
        })
    return [enter]


def generate_holding_rows(
    symbol: str,
    trend: SignalTrend,
    stop_zone: tuple[float, float],
    support_zone: tuple[float, float],
    resistance_zone: tuple[float, float],
    parent_id: str,
    created_at: datetime,
    profit_mode: str = 'fixed',
) -> list[Signal]:
    rows = [
        _signal(
            f'{created_at.date().isoformat()}-{symbol}-stop', 'holding',
            symbol, 'SELL', SignalTag.STOP, stop_zone, trend, created_at,
            parent_id, SignalValidPolicy.UNTIL_STOPPED,
        )
    ]
    if trend is SignalTrend.UP:
        rows.extend([
            _signal(
                f'{created_at.date().isoformat()}-{symbol}-profit', 'holding',
            symbol, 'SELL', SignalTag.PROFIT, resistance_zone, trend,
                created_at, parent_id, SignalValidPolicy.UNTIL_STOPPED,
            ),
            _signal(
                f'{created_at.date().isoformat()}-{symbol}-t-buy', 'holding',
                symbol, 'BUY', SignalTag.T, support_zone, trend, created_at, parent_id,
                SignalValidPolicy.SAME_DAY,
            ),
            _signal(
                f'{created_at.date().isoformat()}-{symbol}-t-sell', 'holding',
                symbol, 'SELL', SignalTag.T, resistance_zone, trend, created_at, parent_id,
                SignalValidPolicy.SAME_DAY,
            ),
        ])
    if rows[-1].tag is SignalTag.PROFIT:
        rows[-1] = rows[-1].model_copy(update={'activation': 'trailing' if profit_mode == 'trailing' else 'profit'})
    return rows


def apply_size_hints(
    signals: Iterable[Signal],
    market_tier: str,
) -> list[Signal]:
    if market_tier.lower() not in {'low', 'normal', 'high'}:
        raise ValueError('market_tier must be low, normal or high')
    multiplier = 0.5 if market_tier.lower() == 'low' else 1.0
    updated: list[Signal] = []
    for signal in signals:
        if signal.side.value == 'BUY' and signal.size_hint is not None:
            updated.append(signal.model_copy(update={'size_hint': max(1, round(signal.size_hint * multiplier))}))
        else:
            updated.append(signal)
    return updated


def order_for_push(signals: Iterable[Signal]) -> list[Signal]:
    return sorted(signals, key=lambda row: (_PRIORITY[row.tag].value, row.id))


def apply_t_size_choice(signal: Signal, choice: TSizeChoice) -> Signal:
    if signal.tag is not SignalTag.T or signal.side.value != 'BUY':
        raise ValueError('t size choice applies only to T BUY rows')
    if signal.size_hint is None or signal.size_hint <= 0:
        raise ValueError('T BUY row requires a positive size_hint')
    multipliers = {
        TSizeChoice.SAME: 1.0,
        TSizeChoice.DOUBLE: 2.0,
        TSizeChoice.HALF: 0.5,
    }
    return signal.model_copy(update={
        'size_hint': max(1, round(signal.size_hint * multipliers[choice]))
    })


def _zone(center: float, width_pct: float) -> tuple[float, float]:
    if center <= 0:
        raise ValueError('zone center must be positive')
    half = center * width_pct
    return round(center - half, 6), round(center + half, 6)


def _break_zone(level: float, width_pct: float) -> tuple[float, float]:
    if level <= 0:
        raise ValueError('break level must be positive')
    return round(level * (1 - width_pct), 6), round(level, 6)


def _upper_zone(level: float, width_pct: float) -> tuple[float, float]:
    if level <= 0:
        raise ValueError('zone level must be positive')
    return round(level, 6), round(level * (1 + width_pct), 6)


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
    ma_source = closes[:-1] if closes and abs(closes[-1] - est_close) < 1e-9 else closes
    y_type = str(y.get('type'))
    if y_type == 'ma_tomorrow':
        line = ma_tomorrow(ma_source, int(y['n']), est_close)
        entry_zone = _upper_zone(line, zone_width_pct)
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
        stop_level = ma_tomorrow(ma_source, n, est_close)
        stop_zone = _break_zone(stop_level, zone_width_pct)
    else:
        raise ValueError(f'unsupported strategy y type: {y_type}')

    if str(w.get('type')) in {'pressure_zone', 'next_resistance'} and resistance:
        profit_zone = (round(resistance * 0.995, 6), round(resistance * 1.005, 6))
    else:
        profit_zone = (round(entry_zone[1] * 1.02, 6), round(entry_zone[1] * 1.04, 6))

    if stop_zone[1] > entry_zone[0]:
        return {}
    return {
        'symbol': symbol,
        'strategy_id': str(_strategy_value(strategy, 'id')),
        'entry_low': entry_zone[0],
        'entry_high': entry_zone[1],
        'invalidation_price': stop_zone[1],
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
