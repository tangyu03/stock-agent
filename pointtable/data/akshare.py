from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

logger = logging.getLogger(__name__)
_CACHE: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}
_CACHE_EXPIRY: dict[tuple[str, tuple[tuple[str, Any], ...]], float] = {}
_CACHE_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix='akshare')
_EXECUTOR_LOCK = threading.Lock()


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
    rows = cached_ak('stock_zh_a_hist', symbol=symbol, period='daily', adjust='qfq')
    return _tail(rows, trade_date, limit)


def index_kline(symbol: str, limit: int = 60) -> list[dict[str, Any]]:
    rows = cached_ak('stock_zh_index_daily', symbol=symbol)
    return _tail(rows, None, limit)


def sector_kline(symbol: str, limit: int = 60) -> list[dict[str, Any]]:
    rows = cached_ak('stock_board_industry_hist_em', symbol=symbol)
    return _tail(rows, None, limit)


def auction_snapshot(symbol: str, trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_zh_a_hist_min_em', symbol=symbol, period='1', adjust='')
    return [row for row in _tail(rows, trade_date, 1)]


def lhb_rows(trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_lhb_detail_em', date=trade_date)
    return _tail(rows, trade_date, 100)


def margin_rows(trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_margin_sse', date=trade_date)
    return _tail(rows, trade_date, 100)


def money_flow(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = cached_ak('stock_individual_fund_flow', stock=symbol, market='sh' if symbol.startswith('6') else 'sz')
    return _tail(rows, None, limit)


def _tail(rows: Any, trade_date: str | None, limit: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    normalized = [dict(row) for row in rows]
    if trade_date:
        normalized = [
            row for row in normalized
            if str(row.get('trade_date') or row.get('日期') or '').replace('-', '') == trade_date.replace('-', '')
        ]
    return normalized[-max(limit, 0):]
