from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core.lhb import evaluate_lhb
from ..core.ma import ma_tomorrow
from ..core.trend import classify_trend, moving_average
from ..core.sector import load_theme_map
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
    watchlist_limit: int | None = None,
    priority_order: Sequence[str] = ('holding', 'index', 'sector', 'candidate'),
    now: date | None = None,
) -> dict[str, Any]:
    theme_map = load_theme_map(Path(__file__).parents[1] / 'config' / 'theme_map.yaml')
    closes = _closes(index_rows)
    if len(closes) < 20:
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
    depths = entry_depths or {}
    assignments: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    stop_zones: dict[str, tuple[float, float]] = {}
    for pool_row in pool_rows:
        symbol = str(pool_row.get('symbol') or '')
        entry_depth = int(depths.get(symbol) or 0)
        if entry_depth >= 2:
            rejected.append({'symbol': symbol, 'reason': 'reentry_limit'})
            continue
        strategy_id = 'first_move_10d' if entry_depth == 1 else 'first_move_5d'
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
        ma_n = 10 if entry_depth == 1 else 5
        if est_close is None or len(plan_closes) < ma_n - 1:
            rejected.append({'symbol': symbol, 'reason': 'insufficient_closes'})
            continue
        stop_line = ma_tomorrow(plan_closes, ma_n, est_close)
        stop_zones[symbol] = _break_zone(stop_line, 0.01)
        regulated = symbol in set(regulator_list)
        bad_lhb, lhb_reason = lhb_decision(lhb, symbol, lhb_dumpers)
        support_zone = _zone(rows, 5)
        resistance_zone = _zone(rows, 20)
        assignments.append({
            'symbol': symbol,
            'strategy_id': strategy_id,
            'entry_depth': entry_depth,
            'parent_id': (entry_parent_ids or {}).get(symbol),
            'closes': plan_closes,
            'est_close': est_close,
            'support': support_zone[0] if support_zone else None,
            'resistance': resistance_zone[1] if resistance_zone else None,
            'premise_facts': {
                'hot_theme': theme != 'unknown',
                'not_regulated': not regulated,
                'lhb_ok': not bad_lhb,
            },
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
        entry_parent_ids=entry_parent_ids,
        watchlist_limit=(
            int(config.get('watchlist_limit'))
            if config.get('watchlist_limit') is not None
            else None
        ),
        priority_order=None,
    )
