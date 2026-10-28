from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
import yaml

from ..core.calendar import is_trading_day, load_holidays
from ..core.generator import (
    Strategy,
    apply_size_hints,
    build_pool_plans,
    _signal,
    generate_holding_rows,
    generate_entry,
    premise_ok,
)
from ..core.lhb import evaluate_lhb
from ..core.ma import ma_tomorrow
from ..core.sector import load_theme_map
from ..core.selector import COOLDOWN_STOP_COUNT, MA_FAMILY, cooled_out, select_strategies
from ..core.trend import classify_trend, moving_average
from ..data.guard import DataFreshnessGuard, check_quote_sanity, check_two_source
from ..db.session import (
    load_signals, save_context_marks, save_event_calendar, save_journal,
    save_market_snapshots, save_report_signal_snapshot, save_signals,
)
from ..models.context_mark import ContextMark, ContextType
from ..models.event_calendar import EventCalendar
from ..models.journal import JournalEntry
from ..models.signal import Signal, SignalRole, SignalSide, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy
from ..notify.render import render_report
from ..stats import signal_quality
from .restore_check import load_tail_prices
from .settle import observation_pause_symbols



def market_tier(amount_yi: float, low_max: float = 9000, high_min: float = 13000) -> str:
    if amount_yi < 0:
        raise ValueError('amount_yi must not be negative')
    if amount_yi < low_max:
        return 'LOW'
    if amount_yi >= high_min:
        return 'HIGH'
    return 'NORMAL'


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
    recent_stop_counts: Mapping[str, Mapping[str, int]] | None = None,
    same_ma_cooldown_days: int = 2,
) -> list[Signal]:
    rows: list[Signal] = []
    active_entries = {
        (signal.symbol, signal.strategy_id)
        for signal in (existing_signals or [])
        if signal.signal_role is SignalRole.ENTRY
        and signal.status in {SignalStatus.ACTIVE, SignalStatus.TRIGGERED}
    }
    for plan in pool_plans:
        strategy = strategies.get(str(plan.get('strategy_id') or ''))
        if strategy is None or not premise_ok(plan.get('premise_facts') or {}):
            continue
        if (str(plan['symbol']), strategy.id) in active_entries:
            continue
        if cooled_out(
            [strategy.id],
            (recent_stop_counts or {}).get(str(plan['symbol']), {}),
            same_ma_cooldown_days,
        ) == []:
            continue
        rows.extend(generate_entry(
            strategy,
            str(plan['symbol']),
            (float(plan['entry_low']), float(plan['entry_high'])),
            created_at,
            parent_id=plan.get('parent_id'),
        ))
    return apply_size_hints(rows, market_tier_name)


def expire_cooled_candidates(
    existing_signals: list[Signal],
    recent_stop_counts: Mapping[str, Mapping[str, int]],
    created_at: datetime,
) -> list[Signal]:
    rows: list[Signal] = []
    for signal in existing_signals:
        family = MA_FAMILY.get(signal.strategy_id, signal.strategy_id)
        hits = int((recent_stop_counts.get(signal.symbol) or {}).get(family) or 0)
        if (
            signal.signal_role is SignalRole.ENTRY
            and signal.status is SignalStatus.ACTIVE
            and hits >= COOLDOWN_STOP_COUNT
        ):
            rows.append(signal.model_copy(update={
                'status': SignalStatus.EXPIRED,
                'status_reason': 'ma_family_cooldown',
                'closed_at': created_at,
            }))
    return rows


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
            'trailing' if holding.get('trailing') else 'fixed',
        )
        if holding.get('volatility_exceeded'):
            generated = [row for row in generated if row.tag is not SignalTag.T]
        rows.extend(generated)
    return rows


def build_pool_signals(
    rows: list[Mapping[str, Any]],
    created_at: datetime,
    existing_signals: list[Signal] | None = None,
) -> list[Signal]:
    result: list[Signal] = []
    existing = list(existing_signals or [])
    trade_date = created_at.date().isoformat()

    for row in rows:
        symbol = str(row.get('symbol') or '')
        strategy_id = str(row.get('strategy_id') or 'pool')
        parent_id = str(row.get('parent_id') or '')
        trend = (
            SignalTrend.UP
            if str(row.get('trend') or 'UP').upper() == 'UP'
            else SignalTrend.DOWN
        )
        if not symbol or not parent_id:
            raise ValueError('pool signals require symbol and parent_id')
        prior_stops = [
            signal for signal in existing
            if signal.symbol == symbol
            and signal.tag is SignalTag.STOP
            and signal.status is SignalStatus.TRIGGERED
        ]
        prior_stop = max(prior_stops, key=lambda signal: signal.closed_at or signal.created_at, default=None)
        if (
            prior_stop is not None
            and prior_stop.break_qualifier == 'EXECUTE_NOW'
            and not any(
                signal.symbol == symbol
                and signal.tag is SignalTag.ENTER
                and signal.status is SignalStatus.TRIGGERED
                and signal.created_at > (prior_stop.closed_at or prior_stop.created_at)
                for signal in existing
            )
        ):
            continue
        for signal in [
            item for item in existing
            if item.symbol == symbol
            and item.parent_id == parent_id
            and item.tag is SignalTag.T
            and item.status is SignalStatus.ACTIVE
        ]:
            result.append(signal.model_copy(update={
                'status': SignalStatus.EXPIRED,
                'status_reason': 'nightly_reset',
                'closed_at': created_at,
            }))

        desired = [
            _signal(
                f'{trade_date}-{symbol}-stop', strategy_id, symbol,
                'SELL', SignalTag.STOP,
                (float(row['stop_zone'][0]), float(row['stop_zone'][1])),
                trend, created_at, parent_id, SignalValidPolicy.UNTIL_STOPPED,
            ),
            _signal(
                f'{trade_date}-{symbol}-profit', strategy_id, symbol,
                'SELL', SignalTag.PROFIT,
                (float(row['profit_zone'][0]), float(row['profit_zone'][1])),
                trend, created_at, parent_id, SignalValidPolicy.UNTIL_STOPPED,
            ),
        ]
        if prior_stop is not None:
            desired = [
                signal.model_copy(update={
                    'premise': {
                        **signal.premise,
                        'prior_stop_id': prior_stop.id,
                    },
                })
                for signal in desired
            ]
        t_buy_zone = (
            float(row['t_buy_zone'][0]), float(row['t_buy_zone'][1]),
        )
        t_sell_zone = (
            float(row['t_sell_zone'][0]), float(row['t_sell_zone'][1]),
        )
        include_t = trend is SignalTrend.UP
        if include_t:
            triggered_stops = [
                item for item in existing
                if item.symbol == symbol
                and item.tag is SignalTag.STOP
                and item.status is SignalStatus.TRIGGERED
                and item.closed_at is not None
                and item.closed_at.date() == created_at.date()
            ]
            active_stops = [
                item for item in existing
                if item.symbol == symbol
                and item.tag is SignalTag.STOP
                and item.status is SignalStatus.ACTIVE
            ]
            overlap_stop = any(
                max(item.zone_low, t_buy_zone[0])
                <= min(item.zone_high, t_buy_zone[1])
                for item in [desired[0], *active_stops]
            )
            include_t = not triggered_stops and not overlap_stop
        if include_t and (
            t_buy_zone[0] <= 0 or t_buy_zone[0] >= t_buy_zone[1]
            or (t_buy_zone[1] - t_buy_zone[0]) / t_buy_zone[0] > 0.15
            or t_sell_zone[0] <= t_buy_zone[1]
        ):
            include_t = False
        if include_t:
            desired.extend([
                _signal(
                    f'{trade_date}-{symbol}-t-buy', strategy_id, symbol,
                    'BUY', SignalTag.T,
                    t_buy_zone,
                    trend, created_at, parent_id, SignalValidPolicy.SAME_DAY,
                ),
                _signal(
                    f'{trade_date}-{symbol}-t-sell', strategy_id, symbol,
                    'SELL', SignalTag.T,
                    t_sell_zone,
                    trend, created_at, parent_id, SignalValidPolicy.SAME_DAY,
                ),
            ])

        if not include_t and trend is SignalTrend.UP:
            if triggered_stops:
                reason = 'same_day_triggered_stop'
            elif overlap_stop:
                reason = 't_buy_overlaps_stop'
            else:
                reason = 'invalid_t_zone'
            desired = [
                signal.model_copy(update={
                    'premise': {**signal.premise, 't_suppression_reason': reason},
                })
                for signal in desired
            ]

        for signal in desired:
            candidates = [
                item for item in existing
                if item.symbol == signal.symbol
                and item.tag is signal.tag
                and item.side is signal.side
                and item.valid_policy is signal.valid_policy
                and item.status is SignalStatus.ACTIVE
            ]
            if candidates and signal.valid_policy is not SignalValidPolicy.SAME_DAY:
                latest = max(candidates, key=lambda item: item.created_at)
                result.append(latest.model_copy(update={
                    'strategy_id': signal.strategy_id,
                    'parent_id': signal.parent_id,
                    'zone_low': signal.zone_low,
                    'zone_high': signal.zone_high,
                    'trend': signal.trend,
                }))
            else:
                result.append(signal)

        if trend is SignalTrend.DOWN:
            for signal in existing:
                if (
                    signal.symbol == symbol
                    and signal.parent_id == parent_id
                    and signal.tag is SignalTag.T
                    and signal.status is SignalStatus.ACTIVE
                ):
                    result.append(signal.model_copy(update={
                        'status': SignalStatus.EXPIRED,
                        'status_reason': 'trend_flip',
                        'closed_at': created_at,
                    }))
    return result


def evening_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
) -> str:
    context = dict(market)
    context.setdefault('missed_stop_hit', any(position.get('missed_stop_date') for position in positions))
    return render_report(report_date, context, positions)


logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)



def _value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def _sorted_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(row) for row in rows]
    return sorted(
        normalized,
        key=lambda row: str(_value(row, 'trade_date', '日期', 'date') or ''),
    )


def _numeric(row: Mapping[str, Any], *keys: str) -> float | None:
    try:
        value = _value(row, *keys)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _closes(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    values = [_numeric(row, '收盘', 'close', 'Close') for row in _sorted_rows(rows)]
    return [value for value in values if value is not None and value > 0]


def _volume_ratio(rows: Sequence[Mapping[str, Any]], trade_date: Any = None) -> float | None:
    '''Daily close volume ratio: today's volume divided by the prior five-day average.'''
    ordered = _sorted_rows(rows)
    if trade_date is not None:
        target = str(trade_date)[:10]
        latest_date = str(_value(ordered[-1] if ordered else {}, 'trade_date', '日期', 'date') or '')
        if latest_date != target:
            return None
    if len(ordered) < 6:
        return None
    latest_volume = _numeric(ordered[-1], '成交量', 'volume', 'Volume')
    prior_volumes = [_numeric(row, '成交量', 'volume', 'Volume') for row in ordered[-6:-1]]
    if latest_volume is None or latest_volume <= 0:
        return None
    if not prior_volumes or any(volume is None or volume <= 0 for volume in prior_volumes):
        return None
    return round(latest_volume / (sum(prior_volumes) / len(prior_volumes)), 6)


def _amount_yi(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return round(amount / 100_000_000 if amount >= 100_000_000 else amount, 2)


def _latest(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    return _sorted_rows(rows)[-1] if rows else None


def _zone(rows: Sequence[Mapping[str, Any]], lookback: int) -> tuple[float, float] | None:
    tail = _sorted_rows(rows)[-lookback:]
    lows = [_numeric(row, '最低', 'low', 'Low') for row in tail]
    highs = [_numeric(row, '最高', 'high', 'High') for row in tail]
    clean_lows = [value for value in lows if value and value > 0]
    clean_highs = [value for value in highs if value and value > 0]
    if not clean_lows or not clean_highs:
        return None
    return round(min(clean_lows), 6), round(max(clean_highs), 6)


def _break_zone(level: float, width_pct: float) -> tuple[float, float]:
    return round(level * (1 - width_pct), 6), round(level, 6)


def volume_breakout_decision(
    rows: Sequence[Mapping[str, Any]],
    est_close: float,
    *,
    volume_ratio_threshold: float = 1.5,
) -> tuple[bool, float | None]:
    ordered = _sorted_rows(rows)
    if len(ordered) < 6 or est_close <= 0:
        return False, None
    prior = ordered[:-1]
    prior_highs = [_numeric(row, '最高', 'high', 'High') for row in prior]
    prior_closes = [_numeric(row, '收盘', 'close', 'Close') for row in prior]
    prior_volumes = [_numeric(row, '成交量', 'volume', 'Volume') for row in prior[-5:]]
    valid_prior_highs = [value for value in prior_highs if value is not None and value > 0]
    if not valid_prior_highs:
        return False, None
    key_level = max(valid_prior_highs)
    prior_close = next((value for value in reversed(prior_closes) if value is not None and value > 0), None)
    if not prior_volumes or any(value is None or value <= 0 for value in prior_volumes):
        return False, key_level
    latest = ordered[-1]
    latest_volume = _numeric(latest, '成交量', 'volume', 'Volume')
    latest_open = _numeric(latest, '开盘', 'open', 'Open')
    if latest_volume is None or latest_volume <= 0:
        return False, key_level
    bullish = est_close > prior_close if latest_open is None else est_close > latest_open
    avg_volume = sum(prior_volumes) / len(prior_volumes)
    return (
        bool(bullish and est_close > key_level and latest_volume >= avg_volume * volume_ratio_threshold),
        key_level,
    )


def lhb_decision(
    rows: Sequence[Mapping[str, Any]],
    symbol: str,
    dumpers: Sequence[str],
    concentration_threshold: float = 0.35,
) -> tuple[bool, str]:
    matched: list[dict[str, Any]] = []
    for row in rows:
        code = str(_value(row, '代码', '股票代码', 'symbol', 'code') or '')
        if symbol in code or symbol in str(row):
            matched.append(dict(row))
    if not matched:
        return False, 'not_on_lhb'

    hits: set[str] = set()
    concentrations: list[float] = []
    for row in matched:
        text_values = [str(value) for value in row.values()]
        for dumper in dumpers:
            if dumper and any(dumper in value for value in text_values):
                hits.add(dumper)
        for key, value in row.items():
            if ('卖出' in str(key) or 'sell' in str(key).lower()) and ('占比' in str(key) or '比例' in str(key) or 'ratio' in str(key).lower()):
                try:
                    ratio = float(value)
                    if ratio > 1:
                        ratio /= 100
                    concentrations.append(ratio)
                except (TypeError, ValueError):
                    continue
    decision = evaluate_lhb(hits, max(concentrations, default=0.0), concentration_threshold)
    return decision.bad, decision.reason


def truncate_watchlist(
    pool_rows: Sequence[Mapping[str, Any]],
    limit: int,
    priority_order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if limit < 0:
        raise ValueError('watchlist limit must not be negative')
    del priority_order
    ranked = sorted(enumerate(pool_rows), key=lambda pair: (str(pair[1].get('symbol') or ''), pair[0]))
    return [dict(row) for _, row in sorted(ranked[:limit])]


def latest_entry_parent_ids(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, str | None]:
    parents: dict[str, str | None] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT id FROM signals WHERE symbol=? AND tag='enter' AND status='TRIGGERED' "
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (symbol,),
        ).fetchone()
        parents[symbol] = str(row['id']) if row else f'pool:{symbol}'
    return parents


def active_retest_symbols(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row['symbol'])
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM signals WHERE tag='retest_confirm' AND status='ACTIVE'"
        ).fetchall()
    }


def recent_stop_counts(
    conn: sqlite3.Connection,
    trade_date: str,
    window_days: int = 2,
    holidays: set[date] | None = None,
) -> dict[str, dict[str, int]]:
    if window_days < 0:
        raise ValueError('window_days must not be negative')
    current = date.fromisoformat(trade_date)
    counted = 0
    while counted < max(0, window_days - 1):
        current -= timedelta(days=1)
        if is_trading_day(current, holidays):
            counted += 1
    threshold = current.isoformat()
    rows = conn.execute(
        '''
        SELECT s.symbol, s.strategy_id AS strategy_id, COUNT(DISTINCT s.id) AS hits
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        WHERE s.tag='stop' AND s.status='TRIGGERED'
          AND j.to_status='TRIGGERED' AND j.created_at >= ?
        GROUP BY s.symbol, s.strategy_id
        ''',
        (threshold + 'T00:00:00',),
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        symbol = str(row['symbol'])
        strategy_id = str(row['strategy_id'] or 'unknown')
        family = MA_FAMILY.get(strategy_id, strategy_id)
        counts.setdefault(symbol, {})[family] = int(row['hits'])
    return counts


def fill_missing_spot_rows(
    spot_rows: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
    quote_supplier: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in spot_rows]
    by_symbol = {str(row.get('symbol')): row for row in rows}
    missing = [symbol for symbol in symbols if not by_symbol.get(symbol, {}).get('price')]
    if missing:
        rows.extend(dict(row) for row in quote_supplier(missing))
    return rows


def build_market_input(
    pool_rows: Sequence[Mapping[str, Any]],
    index_rows: Sequence[Mapping[str, Any]],
    lhb: Sequence[Mapping[str, Any]],
    stock_klines: Mapping[str, Sequence[Mapping[str, Any]]],
    sector_klines: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    regulator_list: Sequence[str] = (),
    lhb_dumpers: Sequence[str] = (),
    tail_prices: Mapping[str, float] | None = None,
    entry_parent_ids: Mapping[str, str | None] | None = None,
    active_retest: set[str] | None = None,
    recent_stops: Mapping[str, Mapping[str, int]] | None = None,
    same_ma_cooldown_days: int = 2,
    lhb_checked: bool = True,
    two_source_deviation_max: float = 0.005,
    quote_sanity_enabled: bool = True,
    deep_pullback_ratio: float = 0.95,
    watchlist_limit: int | None = None,
    priority_order: Sequence[str] = ('holding', 'index', 'sector', 'candidate'),
    now: date | None = None,
    holidays: set[date] | None = None,
    name_map: Mapping[str, str] | None = None,
    active_deltas: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    theme_map = load_theme_map(Path(__file__).parents[2] / 'config' / 'theme_map.yaml')
    closes = _closes(index_rows)
    if len(closes) < 20:
        logger.warning(
            'index closes insufficient: rows=%s first=%s last=%s',
            len(index_rows),
            index_rows[0] if index_rows else None,
            index_rows[-1] if index_rows else None,
        )
        raise ValueError('index klines must contain at least 20 closes')
    latest = _latest(index_rows) or {}
    latest_index_date = str(_value(latest, 'trade_date', '日期', 'date') or '')[:10]
    if now and latest_index_date != str(now):
        raise ValueError(
            f'index kline freshness mismatch: signal_date={now}, index_date={latest_index_date}'
        )
    amount_yi = _amount_yi(_value(latest, 'amount', '成交额'))
    if amount_yi is None:
        raise ValueError('index amount is missing')
    sh_close = closes[-1]
    sh_ma5 = moving_average(closes, 5)
    index_trend = classify_trend(closes).value
    market_tier = 'LOW' if amount_yi < 9000 else 'HIGH' if amount_yi >= 13000 else 'NORMAL'

    for theme, rows in sector_klines.items():
        latest_sector_date = str(
            _value(_latest(rows) or {}, 'trade_date', '日期', 'date') or ''
        )[:10]
        if now and latest_sector_date > str(now):
            raise ValueError(
                f'sector kline is in the future: theme={theme}, sector_date={latest_sector_date}'
            )

    index_zone = _zone(index_rows, 5)
    index_context = [{'symbol': 'sh000001', 'zone': index_zone}] if index_zone else []
    sector_context: list[dict[str, Any]] = []
    themes = sorted({str(row.get('theme_label') or '') for row in pool_rows} - {'', 'unknown'})
    for theme in themes:
        zone = _zone(sector_klines.get(theme, []), 10)
        if zone:
            sector_context.append({'symbol': theme, 'zone': zone})

    guard = DataFreshnessGuard(max_staleness_days=5)
    if watchlist_limit is not None and watchlist_limit < len(pool_rows):
        pool_rows = truncate_watchlist(pool_rows, watchlist_limit, priority_order)
    assignments: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    stop_zones: dict[str, tuple[float, float]] = {}
    pool_signal_rows: list[dict[str, Any]] = []
    pool_total = len(pool_rows)
    for pool_row in pool_rows:
        symbol = str(pool_row.get('symbol') or '')
        if symbol in (active_retest or set()):
            rejected.append({'symbol': symbol, 'reason': 'active_retest_confirm'})
            continue
        theme = theme_map.get(symbol) or str(pool_row.get('theme_label') or 'unknown')
        rows = stock_klines.get(symbol, [])
        if not rows:
            rejected.append({'symbol': symbol, 'reason': 'missing_kline'})
            continue
        latest_stock = _latest(rows) or {}
        latest_date_text = str(_value(latest_stock, 'trade_date', '日期', 'date') or '')[:10]
        try:
            latest_date = date.fromisoformat(latest_date_text)
        except ValueError:
            rejected.append({'symbol': symbol, 'reason': 'stale_kline'})
            continue
        if now and latest_date > now:
            rejected.append({'symbol': symbol, 'reason': 'stale_kline'})
            continue
        if not guard.check_fresh_enough(latest_date, now=now):
            rejected.append({'symbol': symbol, 'reason': 'stale_kline'})
            continue
        stock_closes = _closes(rows)
        tail_price = (tail_prices or {}).get(symbol)
        latest_close = _numeric(latest_stock, '收盘', 'close', 'Close')
        two_source_anomaly = False
        if tail_price is not None:
            est_close = tail_price
            two_source_anomaly = False
            if latest_date.isoformat() == now.isoformat():
                plan_closes = [*stock_closes[:-1], tail_price]
                if latest_close is not None and not check_two_source(
                    tail_price,
                    latest_close,
                    max_deviation=two_source_deviation_max,
                ).ok:
                    two_source_anomaly = True
            else:
                plan_closes = [*stock_closes, tail_price]
        elif latest_date.isoformat() == now.isoformat():
            est_close = latest_close
            plan_closes = stock_closes
        else:
            est_close = None
            plan_closes = []
        if est_close is None:
            rejected.append({'symbol': symbol, 'reason': 'insufficient_closes'})
            continue
        quote_high = _numeric(latest_stock, '最高', 'high', 'High')
        quote_low = _numeric(latest_stock, '最低', 'low', 'Low')
        if quote_sanity_enabled and quote_high is not None and quote_low is not None:
            quote = check_quote_sanity(
                est_close,
                open_price=_numeric(latest_stock, '开盘', 'open', 'Open'),
                high=quote_high,
                low=quote_low,
                prev_close=_numeric(latest_stock, '昨收', 'prev_close', 'previous_close'),
            )
            if not quote.ok:
                rejected.append({'symbol': symbol, 'reason': quote.reason})
                continue
        else:
            quote_high = est_close
            quote_low = est_close
        regulated = symbol in set(regulator_list)
        bad_lhb, lhb_reason = lhb_decision(lhb, symbol, lhb_dumpers)
        if not lhb_checked:
            rejected.append({'symbol': symbol, 'reason': 'lhb_data_unavailable'})
            continue
        breakout_hit, breakout_price = volume_breakout_decision(rows, est_close)
        support_zone = _zone(rows, 5)
        resistance_zone = _zone(rows, 20)
        pool_trend = classify_trend(stock_closes)
        stop_source = (
            plan_closes[:-1] if plan_closes and abs(plan_closes[-1] - est_close) < 1e-9 else plan_closes
        )
        support_center = support_zone[0] if support_zone else est_close * 0.95
        pool_stop_zone = (
            _break_zone(ma_tomorrow(stop_source, 5, est_close), 0.01)
            if pool_trend is SignalTrend.UP
            else _break_zone(support_center, 0.01)
        )
        pool_profit_center = resistance_zone[1] if resistance_zone else est_close * 1.03
        pool_t_buy_center = support_zone[0] if support_zone else est_close * 0.98
        pool_profit_zone = (round(pool_profit_center * 0.995, 6), round(pool_profit_center * 1.005, 6))
        pool_t_buy_zone = (round(pool_t_buy_center * 0.99, 6), round(pool_t_buy_center * 1.01, 6))
        pool_t_sell_zone = pool_profit_zone
        pool_signal_rows.append({
            'symbol': symbol,
            'strategy_id': 'pool',
            'parent_id': (entry_parent_ids or {}).get(symbol),
            'stop_zone': pool_stop_zone,
            'profit_zone': pool_profit_zone,
            't_buy_zone': pool_t_buy_zone,
            't_sell_zone': pool_t_sell_zone,
            'trend': pool_trend.value,
            'est_close': est_close,
            'low': quote_low,
            'high': quote_high,
            'volume_ratio': _numeric(latest_stock, '量比') if _numeric(latest_stock, '量比') is not None else _volume_ratio(rows, now),
            'active_delta': (active_deltas or {}).get(symbol),
        })
        common_facts = {
            'hot_theme': theme != 'unknown',
            'not_regulated': not regulated,
            'lhb_ok': not bad_lhb,
        }
        stock_highs = [
            value
            for value in (
                _numeric(row, '最高', 'high', 'High')
                for row in _sorted_rows(rows)[-10:]
            )
            if value is not None and value > 0
        ]
        high_10 = max(stock_highs, default=0.0)
        ma5_now = moving_average(stock_closes, 5)
        ma5_prev = moving_average(stock_closes[:-1], 5)
        ma10_now = moving_average(stock_closes, 10)
        ma10_prev = moving_average(stock_closes[:-1], 10)
        first_board_raw = pool_row.get('first_board_date') or latest_stock.get('first_board_date')
        first_board = False
        if first_board_raw:
            try:
                first_board_date = date.fromisoformat(str(first_board_raw)[:10])
                next_day = first_board_date + timedelta(days=1)
                while not is_trading_day(next_day, holidays):
                    next_day += timedelta(days=1)
                first_board = bool(
                    (pool_row.get('first_board') or latest_stock.get('first_board'))
                    and now == next_day
                )
            except ValueError:
                first_board = False
        selected = select_strategies({
            **common_facts,
            'ma5_rising': ma5_now is not None and ma5_prev is not None and ma5_now > ma5_prev,
            'deep_pullback': high_10 > 0 and est_close <= high_10 * deep_pullback_ratio,
            'ma10_alive': ma10_now is not None and ma10_prev is not None and ma10_now >= ma10_prev,
            'ma5_falling': ma5_now is not None and ma5_prev is not None and ma5_now < ma5_prev,
            'drift_breakout': (
                ma5_now is not None
                and est_close >= ma_tomorrow(
                    plan_closes[:-1] if plan_closes and abs(plan_closes[-1] - est_close) < 1e-9 else plan_closes,
                    5,
                    est_close,
                )
            ),
            'first_board': first_board,
            'near_support': bool(support_zone and est_close <= support_zone[0] * 1.02),
            'volume_breakout': breakout_hit,
        })
        selected = cooled_out(selected, (recent_stops or {}).get(symbol, {}), same_ma_cooldown_days)
        if not selected:
            rejected.append({'symbol': symbol, 'reason': 'no_strategy'})
            continue
        for strategy_id in selected:
            ma_n = 10 if strategy_id == 'first_move_10d' else 5
            if len(plan_closes) < ma_n - 1:
                continue
            stop_source = plan_closes[:-1] if plan_closes and abs(plan_closes[-1] - est_close) < 1e-9 else plan_closes
            stop_line = ma_tomorrow(stop_source, ma_n, est_close)
            stop_zones[symbol] = _break_zone(stop_line, 0.01)
            assignments.append({
                'symbol': symbol,
                'strategy_id': strategy_id,
                'parent_id': (entry_parent_ids or {}).get(symbol),
                'closes': plan_closes,
                'est_close': est_close,
                'low': quote_low,
                'high': quote_high,
                'two_source_anomaly': two_source_anomaly,
                'support': support_zone[0] if support_zone else None,
                'resistance': resistance_zone[1] if resistance_zone else None,
                'breakout_price': breakout_price,
                'premise_facts': common_facts,
            })
        if selected and pool_signal_rows and pool_signal_rows[-1]['symbol'] == symbol:
            latest_pool_row = pool_signal_rows[-1]
            latest_pool_row['strategy_id'] = selected[0]
            if latest_pool_row['trend'] == SignalTrend.UP.value and symbol in stop_zones:
                latest_pool_row['stop_zone'] = stop_zones[symbol]
    return {
        'trade_date': str(now) if now else str(_value(_latest(index_rows) or {}, 'trade_date', '日期', 'date') or ''),
        'market_tier': market_tier,
        'market': {
            'amount_yi': amount_yi,
            'tier': market_tier,
            'sh_close': sh_close,
            'sh_ma5': sh_ma5,
            'index_trend': index_trend,
        },
        'index_context': index_context,
        'sector_context': sector_context,
        'stop_zones': stop_zones,
        'assignments': assignments,
        'rejected': rejected,
        'lhb_checked': lhb_checked,
        'pool_total': pool_total,
        'watchlist_limit': watchlist_limit,
        'same_ma_cooldown_days': int(same_ma_cooldown_days),
        'recent_stop_counts': dict(recent_stops or {}),
        'name_map': dict(name_map or {}),
        'signal_rows': pool_signal_rows,
    }


def load_risk_config(config_path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding='utf-8')) or {}
    return {
        'regulator_list': raw.get('regulator_list') or [],
        'lhb_dumpers': raw.get('lhb_dumpers') or [],
        'watchlist_limit': raw.get('watchlist_limit'),
        'priority': raw.get('priority') or ['holding', 'index', 'sector', 'candidate'],
        'context_indices': (raw.get('market_context') or {}).get('indices') or ['sh000001'],
        'same_ma_cooldown_days': int(raw.get('reentry_rules', {}).get('same_ma_cooldown_days', 2)),
        'stats': raw.get('stats') or {},
        'guard': raw.get('guard') or {},
        'retest_window_days': int(raw.get('selector', {}).get('retest_window_days', 3)),
        'retest_volume_min': float(raw.get('selector', {}).get('retest_volume_min', 1.2)),
        'holidays': load_holidays(raw.get('holidays')),
    }


def fetch_market_input(
    conn: sqlite3.Connection,
    trade_date: str,
    config_path: str | Path,
) -> dict[str, Any]:

    from ..data import akshare

    pool_rows = [
        dict(row) for row in conn.execute(
        '''
        SELECT symbol, theme_label FROM pool
        WHERE date = (SELECT MAX(date) FROM pool WHERE date <= ?)
        ORDER BY symbol
        ''',
        (trade_date,),
        ).fetchall()
    ]
    symbols = [row['symbol'] for row in pool_rows]
    entry_parent_ids = latest_entry_parent_ids(conn, symbols)
    stock_klines = {symbol: akshare.stock_kline(symbol, trade_date=trade_date, limit=60) for symbol in symbols}
    active_deltas = (
        akshare.stock_active_deltas(symbols)
        if trade_date == date.today().isoformat()
        else {}
    )
    spot_rows = akshare.stock_spot_quotes(symbols)
    name_map = {
        str(row['symbol']): str(row.get('name') or '')
        for row in spot_rows
        if row.get('symbol') and row.get('name')
    }
    themes = sorted({row['theme_label'] for row in pool_rows} - {'unknown'})
    sector_klines = {theme: akshare.sector_kline(theme, limit=60) for theme in themes}
    config = load_risk_config(config_path)
    payload = build_market_input(
        pool_rows,
        akshare.index_kline('sh000001', trade_date=trade_date, limit=60),
        akshare.lhb_rows(trade_date),
        stock_klines,
        sector_klines,
        regulator_list=config['regulator_list'],
        lhb_dumpers=config['lhb_dumpers'],
        tail_prices=load_tail_prices(conn, trade_date),
        now=date.fromisoformat(trade_date),
        active_retest=active_retest_symbols(conn),
        recent_stops=recent_stop_counts(
            conn, trade_date, config['same_ma_cooldown_days'], config['holidays']
        ),
        same_ma_cooldown_days=config['same_ma_cooldown_days'],
        two_source_deviation_max=float(config.get('guard', {}).get('two_source_deviation_max', 0.005)),
        quote_sanity_enabled=config.get('guard', {}).get('quote_sanity', 'enabled') == 'enabled',
        entry_parent_ids=entry_parent_ids,
        watchlist_limit=(
            int(config.get('watchlist_limit'))
            if config.get('watchlist_limit') is not None
            else None
        ),
        priority_order=(
            config['priority'] if config.get('priority') else None
        ),
        holidays=config['holidays'],
        name_map=name_map,
        active_deltas=active_deltas,
    )
    available_sectors = {
        str(row['symbol']) for row in payload.get('sector_context') or []
    }
    index_summary = [{
        'symbol': 'sh000001',
        'close': payload['market']['sh_close'],
        'ma5': payload['market']['sh_ma5'],
    }]
    for context_index in config['context_indices']:
        if context_index == 'sh000001' or any(
            str(row.get('symbol')) == context_index
            for row in payload.get('index_context') or []
        ):
            continue
        context_rows = akshare.index_kline(context_index, trade_date=trade_date, limit=60)
        context_zone = _zone(
            context_rows,
            5,
        )
        if context_zone:
            payload.setdefault('index_context', []).append({
                'symbol': context_index,
                'zone': context_zone,
            })
        context_closes = _closes(context_rows)
        if context_closes:
            index_summary.append({
                'symbol': context_index,
                'close': context_closes[-1],
                'ma5': moving_average(context_closes, 5),
            })
    payload['market']['index_summary'] = index_summary
    latest_marks: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        '''
        SELECT date, code, level_low, level_high, label
        FROM context_marks
        WHERE context_type='sector' AND status='ACTIVE' AND date <= ?
        ORDER BY date, created_at
        ''',
        (trade_date,),
    ).fetchall():
        code = str(row['code'])
        latest_marks[code] = row
    for theme in themes:
        if theme in available_sectors or theme not in latest_marks:
            continue
        mark = latest_marks[theme]
        payload.setdefault('sector_context', []).append({
            'symbol': theme,
            'zone': [float(mark['level_low']), float(mark['level_high'])],
            'label': 'stale_sector_context',
        })
    return payload


def signal_day_trigger_review(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            COALESCE(t.signal_id, s.id) AS id,
            COALESCE(t.symbol, s.symbol) AS symbol,
            COALESCE(t.strategy_id, s.strategy_id) AS strategy_id,
            COALESCE(t.tag, s.tag) AS tag,
            COALESCE(t.side, s.side) AS side,
            s.status,
            COALESCE(t.zone_low, s.zone_low) AS zone_low,
            COALESCE(t.zone_high, s.zone_high) AS zone_high,
            COALESCE(t.parent_id, s.parent_id) AS parent_id,
            COALESCE(t.created_at, s.created_at) AS signal_created_at,
            j.created_at AS triggered_at
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        LEFT JOIN signal_trigger_snapshots t
          ON t.signal_id = s.id AND t.triggered_at = j.created_at
        WHERE j.to_status='TRIGGERED' AND j.created_at LIKE ?
        ORDER BY j.created_at, s.id
        ''',
        (trade_date + '%',),
    ).fetchall()

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        if str(row['id']) in {item['signal_id'] for item in reviewed}:
            continue
        mechanical = str(row['side']) == 'SELL'
        reviewed.append({
            'signal_id': str(row['id']),
            'symbol': str(row['symbol']),
            'strategy_id': str(row['strategy_id']),
            'tag': str(row['tag']),
            'zone_low': row['zone_low'],
            'zone_high': row['zone_high'],
            'parent_id': row['parent_id'],
            'created_at': row['signal_created_at'],
            'triggered_at': str(row['triggered_at']),
        })
    return reviewed


def run_evening(
    conn: sqlite3.Connection,
    input_path: str | Path,
    report_date: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding='utf-8'))
    trade_date = str(payload['trade_date'])
    created_at = datetime.fromisoformat(str(payload.get('created_at') or trade_date + 'T15:30:00+08:00'))
    strategies_path = payload.get('strategies_path') or Path(__file__).parents[2] / 'config' / 'strategies.yaml'

    from ..core.generator import load_strategies

    strategies = {strategy.id: strategy for strategy in load_strategies(strategies_path)}
    existing_signals = load_signals(conn)
    paused_symbols = observation_pause_symbols(conn)
    plans = list(payload.get('plans') or [])
    assignments = list(payload.get('assignments') or [])
    holdings = [
        row for row in (payload.get('holdings') or [])
        if str(row.get('symbol') or '') not in paused_symbols
    ]
    holding_rows = build_holding_rows(holdings, created_at)
    if assignments:
        plans.extend(build_pool_plans(strategies.values(), assignments))
    plans = [
        plan for plan in plans
        if str(plan.get('symbol') or '') not in paused_symbols
    ]
    rows = build_evening_signals(
        strategies,
        plans,
        str(payload.get('market_tier') or 'NORMAL'),
        created_at,
        existing_signals,
        payload.get('recent_stop_counts') or {},
        int(payload.get('same_ma_cooldown_days') or 2),
    )
    cooled_candidates = expire_cooled_candidates(
        existing_signals,
        payload.get('recent_stop_counts') or {},
        created_at,
    )
    rows.extend(cooled_candidates)
    rows.extend(holding_rows)
    rows.extend(build_pool_signals(
        [
            row for row in (payload.get('signal_rows') or [])
            if str(row.get('symbol') or '') not in paused_symbols
        ],
        created_at,
        existing_signals,
    ))
    context_marks = build_context_marks(
        payload.get('index_context') or [],
        payload.get('sector_context') or [],
        date.fromisoformat(trade_date),
        created_at,
    )
    conn.execute(
        "UPDATE context_marks SET status='INACTIVE' WHERE date < ? AND status='ACTIVE'",
        (trade_date,),
    )
    save_context_marks(conn, context_marks)
    save_event_calendar(conn, build_event_calendar(
        strategies, plans, date.fromisoformat(trade_date), created_at,
    ))
    quality_rows = [
        dict(row)
        for row in conn.execute(
            'SELECT id, symbol, strategy_id, tag, role, status, zone_low, zone_high, created_at FROM signals'
        ).fetchall()
    ]
    market_rows = [
        {
            **{key: assignment.get(key) for key in ('symbol', 'est_close', 'low', 'high', 'two_source_anomaly')},
            'trade_date': trade_date,
            'volume_ratio': assignment.get('volume_ratio'),
        }
        for assignment in assignments
    ]
    save_market_snapshots(conn, trade_date, market_rows)
    ev_negative = {
        strategy_id
        for strategy_id, quality in signal_quality(quality_rows, market_rows).items()
        if quality['ev_negative']
    }
    rows = [
        signal.model_copy(update={'premise': {**signal.premise, 'ev_negative': True}})
        if signal.strategy_id in ev_negative
        else signal
        for signal in rows
    ]
    save_signals(conn, rows)
    for signal in cooled_candidates:
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status=SignalStatus.ACTIVE.value,
            to_status=SignalStatus.EXPIRED.value,
            reason='ma_family_cooldown',
            created_at=created_at,
        ))
    snapshot_signals = [
        signal for signal in load_signals(conn)
        if signal.status is SignalStatus.ACTIVE
        or (
            signal.closed_at is not None
            and signal.closed_at.date() >= date.fromisoformat(trade_date)
        )
    ]
    save_report_signal_snapshot(
        conn,
        report_date or trade_date,
        snapshot_signals,
    )
    conn.commit()
    return {
        'trade_date': trade_date,
        'report_date': report_date or trade_date,
        'signal_ids': [signal.id for signal in rows],
    }
