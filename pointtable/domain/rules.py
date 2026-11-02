"""晚间评估的领域规则：K 线行语义、候选评估循环、量能档位——全部纯函数。

本模块不碰网络、不碰数据库；行集/参数进，结构化结果出。
作业编排（取数、落库）留在 jobs/evening.py。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..core.calendar import is_trading_day
from ..core.lhb import evaluate_lhb
from ..core.ma import ma_tomorrow
from ..core.selector import cooled_out, select_strategies
from ..core.trend import classify_trend, moving_average
from ..data.guard import DataFreshnessGuard, check_quote_sanity, check_two_source
from ..models.signal import SignalTrend

logger = logging.getLogger(__name__)

# §5 量能档位口径：两市合计（沪 + 深）成交额，单位亿元。档位只按合计计算，
# 不得把单一指数行情的 amount 当作两市量能。
VOLUME_TIER_LOW_MAX = 9000
VOLUME_TIER_HIGH_MIN = 13000
# 深市量能口径取深证综指 sz399106（深市全样本，与上证指数对等）；
# 深证成指 sz399001 只有 500 只成分股，其成交额≠深市总额，不得用于档位计算。
SZ_AMOUNT_INDEX = 'sz399106'


def market_tier(
    amount_yi: float,
    low_max: float = VOLUME_TIER_LOW_MAX,
    high_min: float = VOLUME_TIER_HIGH_MIN,
) -> str:
    if amount_yi < 0:
        raise ValueError('amount_yi must not be negative')
    if amount_yi < low_max:
        return 'LOW'
    if amount_yi >= high_min:
        return 'HIGH'
    return 'NORMAL'


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


@dataclass(frozen=True)
class CandidateContext:
    '''候选评估循环的外围输入。字段默认值与 build_market_input 的签名默认值一致。'''
    now: date | None = None
    theme_map: Mapping[str, str] = field(default_factory=dict)
    regulator_list: Sequence[str] = ()
    lhb: Sequence[Mapping[str, Any]] = ()
    lhb_dumpers: Sequence[str] = ()
    lhb_checked: bool = True
    tail_prices: Mapping[str, float] = field(default_factory=dict)
    entry_parent_ids: Mapping[str, str | None] = field(default_factory=dict)
    active_retest: set[str] = field(default_factory=set)
    recent_stops: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    same_ma_cooldown_days: int = 2
    two_source_deviation_max: float = 0.005
    quote_sanity_enabled: bool = True
    deep_pullback_ratio: float = 0.95
    holidays: set[date] = field(default_factory=set)
    active_deltas: Mapping[str, float] = field(default_factory=dict)


def evaluate_candidates(
    pool_rows: Sequence[Mapping[str, Any]],
    stock_klines: Mapping[str, Sequence[Mapping[str, Any]]],
    ctx: CandidateContext,
) -> dict[str, Any]:
    '''逐票评估池内候选：拒绝对账 + 指派 + 池内保护行参数。纯函数。

    返回 {'assignments', 'rejected', 'stop_zones', 'pool_signal_rows', 'pool_total'}。
    '''
    guard = DataFreshnessGuard(max_staleness_days=5)
    assignments: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    stop_zones: dict[str, tuple[float, float]] = {}
    pool_signal_rows: list[dict[str, Any]] = []
    pool_total = len(pool_rows)
    for pool_row in pool_rows:
        symbol = str(pool_row.get('symbol') or '')
        if symbol in ctx.active_retest:
            rejected.append({'symbol': symbol, 'reason': 'active_retest_confirm'})
            continue
        theme = ctx.theme_map.get(symbol) or str(pool_row.get('theme_label') or 'unknown')
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
        if ctx.now and latest_date > ctx.now:
            rejected.append({'symbol': symbol, 'reason': 'stale_kline'})
            continue
        if not guard.check_fresh_enough(latest_date, now=ctx.now):
            rejected.append({'symbol': symbol, 'reason': 'stale_kline'})
            continue
        stock_closes = _closes(rows)
        tail_price = ctx.tail_prices.get(symbol)
        latest_close = _numeric(latest_stock, '收盘', 'close', 'Close')
        two_source_anomaly = False
        if tail_price is not None:
            est_close = tail_price
            two_source_anomaly = False
            if latest_date.isoformat() == ctx.now.isoformat():
                plan_closes = [*stock_closes[:-1], tail_price]
                if latest_close is not None and not check_two_source(
                    tail_price,
                    latest_close,
                    max_deviation=ctx.two_source_deviation_max,
                ).ok:
                    two_source_anomaly = True
            else:
                plan_closes = [*stock_closes, tail_price]
        elif latest_date.isoformat() == ctx.now.isoformat():
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
        if ctx.quote_sanity_enabled and quote_high is not None and quote_low is not None:
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
        regulated = symbol in set(ctx.regulator_list)
        bad_lhb, lhb_reason = lhb_decision(ctx.lhb, symbol, ctx.lhb_dumpers)
        if not ctx.lhb_checked:
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
            'parent_id': ctx.entry_parent_ids.get(symbol),
            'stop_zone': pool_stop_zone,
            'profit_zone': pool_profit_zone,
            't_buy_zone': pool_t_buy_zone,
            't_sell_zone': pool_t_sell_zone,
            'trend': pool_trend.value,
            'est_close': est_close,
            'low': quote_low,
            'high': quote_high,
            'volume_ratio': _numeric(latest_stock, '量比') if _numeric(latest_stock, '量比') is not None else _volume_ratio(rows, ctx.now),
            'active_delta': ctx.active_deltas.get(symbol),
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
                while not is_trading_day(next_day, ctx.holidays):
                    next_day += timedelta(days=1)
                first_board = bool(
                    (pool_row.get('first_board') or latest_stock.get('first_board'))
                    and ctx.now == next_day
                )
            except ValueError:
                first_board = False
        selected = select_strategies({
            **common_facts,
            'ma5_rising': ma5_now is not None and ma5_prev is not None and ma5_now > ma5_prev,
            'deep_pullback': high_10 > 0 and est_close <= high_10 * ctx.deep_pullback_ratio,
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
        selected = cooled_out(selected, ctx.recent_stops.get(symbol, {}), ctx.same_ma_cooldown_days)
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
                'parent_id': ctx.entry_parent_ids.get(symbol),
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
        'assignments': assignments,
        'rejected': rejected,
        'stop_zones': stop_zones,
        'pool_signal_rows': pool_signal_rows,
        'pool_total': pool_total,
    }
