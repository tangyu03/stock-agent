from __future__ import annotations

import logging
import re
import requests
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)
_CACHE: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}
_CACHE_EXPIRY: dict[tuple[str, tuple[tuple[str, Any], ...]], float] = {}
_CACHE_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix='akshare')
_EXECUTOR_LOCK = threading.Lock()
_TENCENT_KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'


def call_ak_with_timeout(func: Callable[..., Any], timeout: float = 15, *args, **kwargs) -> Any | None:
    with _EXECUTOR_LOCK:
        future = _EXECUTOR.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        logger.warning('akshare call timed out: %s', getattr(func, '__name__', func))
        return None
    except Exception as exc:
        logger.warning('akshare call failed: %s: %s', getattr(func, '__name__', func), exc)
        return None


def call_ak_with_retry(
    func: Callable[..., Any],
    timeout: float = 15,
    retries: int = 1,
    backoff: float = 0.5,
    *args,
    **kwargs,
) -> Any | None:
    for attempt in range(retries + 1):
        result = call_ak_with_timeout(func, timeout, *args, **kwargs)
        if result is not None:
            return result
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    return None


def safe_ak(name: str, timeout: float = 15, retries: int = 1, **kwargs) -> Any | None:
    import akshare as ak

    func = getattr(ak, name, None)
    if func is None:
        logger.error('akshare function not found: %s', name)
        return None
    return call_ak_with_retry(func, timeout, retries, **kwargs)


def cached_ak(name: str, ttl_seconds: float = 300, timeout: float = 15, retries: int = 1, **kwargs) -> Any | None:
    key = (name, tuple(sorted(kwargs.items())))
    now = time.monotonic()
    with _CACHE_LOCK:
        expires_at = _CACHE_EXPIRY.get(key)
        if expires_at is not None and expires_at > now and key in _CACHE:
            return _CACHE[key]
    result = safe_ak(name, timeout=timeout, retries=retries, **kwargs)
    with _CACHE_LOCK:
        if result is not None:
            _CACHE[key] = result
            _CACHE_EXPIRY[key] = time.monotonic() + ttl_seconds
    return result


def clear_ak_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_EXPIRY.clear()


def stock_kline(symbol: str, trade_date: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    kwargs = {'symbol': symbol, 'period': 'daily', 'adjust': 'qfq'}
    end = date.fromisoformat(trade_date) if trade_date else date.today()
    start = end - timedelta(days=180)
    normalized = _tencent_kline(
        symbol,
        start.isoformat().replace('-', ''),
        end.isoformat().replace('-', ''),
        limit,
    )
    if normalized and len(normalized) >= min(limit, 20):
        return normalized
    if trade_date:
        kwargs['start_date'] = start.isoformat().replace('-', '')
        kwargs['end_date'] = end.isoformat().replace('-', '')
    rows = cached_ak('stock_zh_a_hist', **kwargs)
    normalized = _records(rows)
    if not normalized:
        prefix = 'sh' if symbol.startswith('6') else 'sz' if symbol.startswith(('0', '3')) else 'bj'
        rows = cached_ak('stock_zh_a_daily', symbol=f'{prefix}{symbol}', start_date=kwargs.get('start_date', ''), end_date=kwargs.get('end_date', ''), adjust='qfq')
        normalized = _records(rows)
    if not normalized:
        normalized = _tencent_kline(
            symbol,
            kwargs.get('start_date') or '',
            kwargs.get('end_date') or date.today().isoformat().replace('-', ''),
            limit,
        )
    if trade_date:
        normalized = [
            row for row in normalized
            if str(row.get('trade_date') or row.get('日期') or '').replace('-', '') <= trade_date.replace('-', '')
        ]
    return normalized[-max(limit, 0):]


def index_kline(symbol: str, trade_date: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    end = date.fromisoformat(trade_date) if trade_date else date.today()
    start = end - timedelta(days=180)
    records = _tencent_kline(
        symbol,
        start.isoformat().replace('-', ''),
        end.isoformat().replace('-', ''),
        limit,
    )
    if records and len(records) >= min(limit, 20) and _latest_amount(records[-1]) is not None:
        return records
    rows = cached_ak(
        'stock_zh_index_daily_em',
        symbol=symbol,
        start_date=start.isoformat().replace('-', ''),
        end_date=end.isoformat().replace('-', ''),
    )
    records = _tail(rows, trade_date, limit, inclusive=True)
    latest_date = records[-1].get('date') or records[-1].get('日期') if records else None
    needs_spot = (
        trade_date is not None
        and records
        and latest_date is not None
        and str(latest_date).replace('-', '') < trade_date.replace('-', '')
    )
    if not records or needs_spot:
        rows = cached_ak('stock_zh_index_daily', symbol=symbol)
        records = _tail(rows, trade_date, limit, inclusive=True)
        latest_date = records[-1].get('date') or records[-1].get('日期') if records else None
    if records and _latest_amount(records[-1]) is None:
        spot_rows = _records(cached_ak('stock_zh_index_spot_sina'))
        spot = next((row for row in spot_rows if str(row.get('代码')) == symbol), None)
        if spot is not None and trade_date:
            spot_date = date.fromisoformat(trade_date)
            latest_date = records[-1].get('date') or records[-1].get('日期')
            if str(latest_date).replace('-', '') == trade_date.replace('-', ''):
                records[-1]['成交额'] = spot.get('成交额')
                records[-1]['close'] = spot.get('最新价') or records[-1].get('close') or records[-1].get('收盘')
                records[-1]['日期'] = spot_date
                records[-1]['date'] = spot_date
    if not records or len(records) < min(limit, 20) or _latest_amount(records[-1]) is None:
        records = _tencent_kline(
            symbol,
            start.isoformat().replace('-', ''),
            end.isoformat().replace('-', ''),
            limit,
        )
        if trade_date and (not records or str(latest_date).replace('-', '') < trade_date.replace('-', '')):
            spot_rows = _records(cached_ak('stock_zh_index_spot_sina'))
            spot = next((row for row in spot_rows if str(row.get('代码')) == symbol), None)
            if spot is not None:
                spot_date = date.fromisoformat(trade_date)
                spot_row = {
                    'date': spot_date,
                    '日期': spot_date,
                    'open': spot.get('今开'),
                    'high': spot.get('最高'),
                    'low': spot.get('最低'),
                    'close': spot.get('最新价'),
                    'volume': spot.get('成交量'),
                    'amount': spot.get('成交额'),
                }
                if records and str(latest_date).replace('-', '') == trade_date.replace('-', ''):
                    records[-1] = spot_row
                else:
                    records.append(spot_row)
                records = records[-max(limit, 0):]
    return records


def sector_kline(symbol: str, limit: int = 60) -> list[dict[str, Any]]:
    rows = cached_ak('stock_board_industry_hist_em', symbol=symbol)
    return _tail(rows, None, limit)


def auction_snapshot(symbol: str, trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_zh_a_hist_min_em', symbol=symbol, period='1', adjust='')
    return [row for row in _tail(rows, trade_date, 1)]


def lhb_rows(trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_lhb_detail_em', start_date=trade_date, end_date=trade_date)
    return _tail(rows, trade_date, 100)


def margin_rows(trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_margin_sse', date=trade_date)
    return _tail(rows, trade_date, 100)


def money_flow(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = cached_ak('stock_individual_fund_flow', stock=symbol, market='sh' if symbol.startswith('6') else 'sz')
    return _tail(rows, None, limit)


def stock_spot_rows() -> list[dict[str, Any]]:
    records = _records(cached_ak('stock_zh_a_spot'))
    result = []
    for row in records:
        code = str(row.get('代码') or '')
        match = re.search(r'\d{6}$', code)
        if not match:
            continue
        result.append({
            'symbol': match.group(),
            'name': row.get('名称'),
            'price': row.get('最新价'),
        })
    return result


def _records(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if hasattr(rows, 'to_dict'):
        records = list(rows.to_dict(orient='records'))
    else:
        records = [dict(row) for row in rows]
    for row in records:
        if 'date' in row and '日期' not in row:
            row['日期'] = row['date']
    return records


def _latest_amount(row: Mapping[str, Any]) -> Any:
    amount = row.get('amount') or row.get('成交额')
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _tencent_symbol(symbol: str) -> str:
    if symbol.startswith(('sh', 'sz', 'bj')):
        return symbol
    prefix = 'sh' if symbol.startswith('6') else 'sz' if symbol.startswith(('0', '3')) else 'bj'
    return f'{prefix}{symbol}'


def _tencent_kline(symbol: str, start_date: str, end_date: str, limit: int) -> list[dict[str, Any]]:
    tencent_symbol = _tencent_symbol(symbol)
    start_date = start_date.replace('-', '')
    end_date = end_date.replace('-', '')
    if len(start_date) == 8:
        start_date = f'{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}'
    if len(end_date) == 8:
        end_date = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}'
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                _TENCENT_KLINE_URL,
                params={
                    'param': f'{tencent_symbol},day,{start_date},{end_date},60,qfq',
                },
                headers={'Referer': 'https://gu.qq.com/'},
                timeout=8,
            )
        response.raise_for_status()
        payload = response.json()
        block = payload.get('data', {}).get(tencent_symbol) or {}
        raw_rows = block.get('qfqday') or block.get('day') or []
        records = [
            {
                'date': str(row[0]),
                'open': float(row[1]),
                'close': float(row[2]),
                'high': float(row[3]),
                'low': float(row[4]),
                'volume': float(row[5]),
            }
            for row in raw_rows
            if len(row) >= 6
        ]
        qt_rows = block.get('qt') or {}
        qt = qt_rows.get(tencent_symbol) or []
        if len(qt) > 37:
            try:
                records[-1]['amount'] = float(qt[37]) * 10_000
            except (TypeError, ValueError):
                pass
        return records[-max(limit, 0):]
    except Exception as exc:
        logger.warning('tencent kline failed: %s: %s', symbol, exc)
        return []


def _tail(rows: Any, trade_date: str | None, limit: int, *, inclusive: bool = False) -> list[dict[str, Any]]:
    normalized = _records(rows)
    if trade_date:
        compare = str.__le__ if inclusive else str.__eq__
        normalized = [
            row for row in normalized
            if compare(str(row.get('trade_date') or row.get('日期') or '').replace('-', ''), trade_date.replace('-', ''))
        ]
    return normalized[-max(limit, 0):]
