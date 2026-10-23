from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

from ..core.lhb import evaluate_lhb
from ..core.calendar import is_trading_day, load_holidays
from ..core.ma import ma_tomorrow
from ..core.trend import classify_trend, moving_average
from ..core.sector import load_theme_map
from ..core.selector import COOLDOWN_STOP_COUNT, MA_FAMILY, cooled_out, select_strategies
from ..data.guard import DataFreshnessGuard
from ..models.signal import SignalTrend


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


def latest_entry_depths(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, int]:
    depths: dict[str, int] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT id, parent_id FROM signals WHERE symbol=? AND tag='enter' "
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (symbol,),
        ).fetchone()
        latest = row
        visited: set[str] = set()
        while latest and latest['id'] not in visited:
            visited.add(latest['id'])
            parent_id = latest['parent_id']
            if not parent_id:
                break
            latest = conn.execute(
                'SELECT id, parent_id FROM signals WHERE id=? AND tag=?',
                (parent_id, 'enter'),
            ).fetchone()
        stop_triggered = bool(latest and conn.execute(
            "SELECT 1 FROM signals WHERE parent_id=? AND tag='stop' AND status='TRIGGERED' LIMIT 1",
            (latest['id'],),
        ).fetchone())
        depths[symbol] = len(visited) if stop_triggered else max(0, len(visited) - 1)
    return depths


def latest_entry_parent_ids(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, str | None]:
    parents: dict[str, str | None] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT parent_id FROM signals WHERE symbol=? AND tag='enter' "
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (symbol,),
        ).fetchone()
        parents[symbol] = row['parent_id'] if row else None
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
        SELECT s.symbol, p.strategy_id AS strategy_id, COUNT(*) AS hits
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        LEFT JOIN signals p ON p.id = s.parent_id
        WHERE s.tag='stop' AND s.status='TRIGGERED'
          AND j.to_status='TRIGGERED' AND j.created_at >= ?
        GROUP BY s.symbol, p.strategy_id
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
    entry_depths: Mapping[str, int] | None = None,
    entry_parent_ids: Mapping[str, str | None] | None = None,
    active_retest: set[str] | None = None,
    recent_stops: Mapping[str, Mapping[str, int]] | None = None,
    same_ma_cooldown_days: int = 2,
    deep_pullback_ratio: float = 0.95,
    watchlist_limit: int | None = None,
    priority_order: Sequence[str] = ('holding', 'index', 'sector', 'candidate'),
    now: date | None = None,
    holidays: set[date] | None = None,
) -> dict[str, Any]:
    theme_map = load_theme_map(Path(__file__).parents[1] / 'config' / 'theme_map.yaml')
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
    amount_yi = _amount_yi(_value(latest, 'amount', '成交额'))
    if amount_yi is None:
        raise ValueError('index amount is missing')
    sh_close = closes[-1]
    sh_ma5 = moving_average(closes, 5)
    index_trend = classify_trend(closes).value
    market_tier = 'LOW' if amount_yi < 9000 else 'HIGH' if amount_yi >= 13000 else 'NORMAL'

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
        if tail_price is not None:
            est_close = tail_price
            if latest_date.isoformat() == now.isoformat():
                plan_closes = [*stock_closes[:-1], tail_price]
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
        regulated = symbol in set(regulator_list)
        bad_lhb, lhb_reason = lhb_decision(lhb, symbol, lhb_dumpers)
        breakout_hit, breakout_price = volume_breakout_decision(rows, est_close)
        support_zone = _zone(rows, 5)
        resistance_zone = _zone(rows, 20)
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
                'entry_depth': int((entry_depths or {}).get(symbol) or 0),
                'parent_id': (entry_parent_ids or {}).get(symbol),
                'closes': plan_closes,
                'est_close': est_close,
                'support': support_zone[0] if support_zone else None,
                'resistance': resistance_zone[1] if resistance_zone else None,
                'breakout_price': breakout_price,
                'premise_facts': common_facts,
            })
    return {
        'trade_date': str(now) if now else str(_value(_latest(index_rows) or {}, 'trade_date', '日期', 'date') or ''),
        'market_tier': market_tier,
        'market': {
            'amount_yi': amount_yi,
            'tier': market_tier,
            'sh_close': sh_close,
            'sh_ma5': sh_ma5,
            'index_trend': index_trend,
            'zero_plan_day': market_tier == 'LOW' and index_trend == SignalTrend.DOWN.value,
        },
        'index_context': index_context,
        'sector_context': sector_context,
        'stop_zones': stop_zones,
        'assignments': assignments,
        'rejected': rejected,
    }


def load_risk_config(config_path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding='utf-8')) or {}
    return {
        'regulator_list': raw.get('regulator_list') or [],
        'lhb_dumpers': raw.get('lhb_dumpers') or [],
        'watchlist_limit': raw.get('watchlist_limit'),
        'priority': raw.get('priority') or ['holding', 'index', 'sector', 'candidate'],
        'same_ma_cooldown_days': int(raw.get('selector', {}).get('same_ma_cooldown_days', 2)),
        'deep_pullback_ratio': float(raw.get('selector', {}).get('deep_pullback_ratio', 0.95)),
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
    from .tail_snapshot import load_tail_prices

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
    entry_depths = latest_entry_depths(conn, symbols)
    entry_parent_ids = latest_entry_parent_ids(conn, symbols)
    stock_klines = {symbol: akshare.stock_kline(symbol, trade_date=trade_date, limit=60) for symbol in symbols}
    themes = sorted({row['theme_label'] for row in pool_rows} - {'unknown'})
    sector_klines = {theme: akshare.sector_kline(theme, limit=60) for theme in themes}
    config = load_risk_config(config_path)
    return build_market_input(
        pool_rows,
        akshare.index_kline('sh000001', trade_date=trade_date, limit=60),
        akshare.lhb_rows(trade_date),
        stock_klines,
        sector_klines,
        regulator_list=config['regulator_list'],
        lhb_dumpers=config['lhb_dumpers'],
        tail_prices=load_tail_prices(conn, trade_date),
        now=date.fromisoformat(trade_date),
        entry_depths=entry_depths,
        active_retest=active_retest_symbols(conn),
        recent_stops=recent_stop_counts(
            conn, trade_date, config['same_ma_cooldown_days'], config['holidays']
        ),
        same_ma_cooldown_days=config['same_ma_cooldown_days'],
        deep_pullback_ratio=config['deep_pullback_ratio'],
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
    )
