from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ..models.signal import Signal, SignalTag, SignalTrend, SignalValidPolicy


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
    stop_zone: tuple[float, float],
    profit_zone: tuple[float, float],
    created_at: datetime,
    trade_date: date | None = None,
    parent_id: str | None = None,
) -> list[Signal]:
    validate_strategy(strategy)
    trade_date = trade_date or created_at.date()
    enter_id = f'{trade_date.isoformat()}-{symbol}-{strategy.id}'
    trend = SignalTrend.UP
    enter = _signal(
        enter_id, strategy.id, symbol, 'BUY', SignalTag.ENTER,
        entry_zone, trend, created_at,
        parent_id,
    )
    stop = _signal(
        f'{enter_id}-z', strategy.id, symbol, 'SELL', SignalTag.STOP,
        stop_zone, trend, created_at, enter_id, SignalValidPolicy.UNTIL_STOPPED,
    )
    profit = _signal(
        f'{enter_id}-w', strategy.id, symbol, 'SELL', SignalTag.PROFIT,
        profit_zone, trend, created_at, enter_id,
    )
    return [enter, stop, profit]


def generate_holding_rows(
    symbol: str,
    trend: SignalTrend,
    stop_zone: tuple[float, float],
    support_zone: tuple[float, float],
    resistance_zone: tuple[float, float],
    parent_id: str,
    created_at: datetime,
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
                f'{created_at.date().isoformat()}-{symbol}-t-buy', 'holding',
                symbol, 'BUY', SignalTag.T, support_zone, trend, created_at, parent_id,
            ),
            _signal(
                f'{created_at.date().isoformat()}-{symbol}-t-sell', 'holding',
                symbol, 'SELL', SignalTag.T, resistance_zone, trend, created_at, parent_id,
            ),
        ])
    return rows


def generate_context_row(
    symbol: str,
    zone: tuple[float, float],
    created_at: datetime,
) -> Signal:
    tag = SignalTag.INDEX_CONTEXT if symbol.startswith(('sh', 'sz')) else SignalTag.SECTOR_CONTEXT
    return _signal(
        f'{created_at.date().isoformat()}-{symbol}-context', 'market_context',
        symbol, 'BUY', tag, zone, SignalTrend.UP, created_at,
    )


def generate_time_trigger_row(
    symbol: str,
    zone: tuple[float, float],
    created_at: datetime,
) -> Signal:
    return _signal(
        f'{created_at.date().isoformat()}-{symbol}-time-trigger', 'calendar',
        symbol, 'BUY', SignalTag.TIME_TRIGGER, zone, SignalTrend.UP, created_at,
    )


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


def can_reenter(parent_chain: Iterable[str], max_depth: int = 2) -> bool:
    return len(list(parent_chain)) < max_depth


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
