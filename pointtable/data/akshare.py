from __future__ import annotations

import logging
import json
import os
import re
import requests
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date, timedelta
from typing import Any
from pathlib import Path

from .normalize import as_records, row_amount, row_compact_date, row_date, trim_to_date
from .normalize import tail as _tail_slice
from .resolvers import AmountFetcher, RowCompletion, RowFetcher, resolve_kline

logger = logging.getLogger(__name__)
_CACHE: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}
_CACHE_EXPIRY: dict[tuple[str, tuple[tuple[str, Any], ...]], float] = {}
_CACHE_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix='akshare')
_EXECUTOR_LOCK = threading.Lock()
_TENCENT_KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
_TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q='
_EASTMONEY_KLINE_HOSTS = (
    'push2his.eastmoney.com',
    '21.push2his.eastmoney.com',
    '90.push2his.eastmoney.com',
)
_TENCENT_QUOTE_URL = 'http://qt.gtimg.cn/q='
_INDEX_AMOUNT_OVERRIDES_PATH = Path(__file__).with_name('index_amount_overrides.json')

for proxy_key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
    os.environ.pop(proxy_key, None)
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'


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


def _stock_rows_tencent(symbol: str, trade_date: str | None, limit: int) -> list[dict[str, Any]]:
    start, end = _kline_window(trade_date)
    return _tencent_kline(
        symbol,
        start.isoformat().replace('-', ''),
        end.isoformat().replace('-', ''),
        limit,
    )


def _stock_rows_em_hist(symbol: str, trade_date: str | None, limit: int) -> Any:
    kwargs: dict[str, Any] = {'symbol': symbol, 'period': 'daily', 'adjust': 'qfq'}
    if trade_date:
        start, end = _kline_window(trade_date)
        kwargs['start_date'] = start.isoformat().replace('-', '')
        kwargs['end_date'] = end.isoformat().replace('-', '')
    return cached_ak('stock_zh_a_hist', **kwargs)


def _stock_rows_sina_daily(symbol: str, trade_date: str | None, limit: int) -> Any:
    start, end = _kline_window(trade_date)
    prefix = 'sh' if symbol.startswith('6') else 'sz' if symbol.startswith(('0', '3')) else 'bj'
    return cached_ak(
        'stock_zh_a_daily',
        symbol=f'{prefix}{symbol}',
        start_date=start.isoformat().replace('-', ''),
        end_date=end.isoformat().replace('-', ''),
        adjust='qfq',
    )


_STOCK_ROW_FETCHERS = (
    RowFetcher(name='tencent_kline', fetch=_stock_rows_tencent),
    RowFetcher(name='em_stock_hist', fetch=_stock_rows_em_hist),
    RowFetcher(name='sina_stock_daily', fetch=_stock_rows_sina_daily),
)


def stock_kline(symbol: str, trade_date: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    '''个股日 K：腾讯 → em 历史 → 新浪日线；链全空才重取腾讯（无成交额语义）。'''
    result = resolve_kline(
        _STOCK_ROW_FETCHERS,
        [],
        [],
        symbol=symbol,
        trade_date=trade_date,
        limit=limit,
        inclusive=True,
        require_amount=False,
        last_resort='empty',
    )
    return result.rows


def _kline_window(trade_date: str | None) -> tuple[date, date]:
    end = date.fromisoformat(trade_date) if trade_date else date.today()
    return end - timedelta(days=180), end


def _index_rows_tencent(symbol: str, trade_date: str | None, limit: int) -> list[dict[str, Any]]:
    start, end = _kline_window(trade_date)
    return _tencent_kline(
        symbol,
        start.isoformat().replace('-', ''),
        end.isoformat().replace('-', ''),
        limit,
    )


def _index_rows_em_hist(symbol: str, trade_date: str | None, limit: int) -> Any:
    start, end = _kline_window(trade_date)
    rows = cached_ak(
        'stock_zh_index_daily_em',
        symbol=symbol,
        start_date=start.isoformat().replace('-', ''),
        end_date=end.isoformat().replace('-', ''),
        timeout=20,
        retries=3,
    )
    if rows is None:
        rows = _eastmoney_index_kline_direct(symbol, start, end)
    return rows


def _index_rows_sina_daily(symbol: str, trade_date: str | None, limit: int) -> Any:
    return cached_ak('stock_zh_index_daily', symbol=symbol)


def _index_amount_overrides(symbol: str, trade_date: str | None, tail_date: str | None) -> float | None:
    # 人工兜底：仅历史日查询（当日由实时链补齐）；条目按 (symbol, trade_date) 键控，日期按构造对账
    if not trade_date or trade_date == date.today().isoformat():
        return None
    if not _INDEX_AMOUNT_OVERRIDES_PATH.exists():
        return None
    override = json.loads(_INDEX_AMOUNT_OVERRIDES_PATH.read_text(encoding='utf-8')).get(symbol) or {}
    amount = (override or {}).get(trade_date)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _index_amount_em_hist(symbol: str, trade_date: str | None, tail_date: str | None) -> float | None:
    # em 历史接口按精确交易日取行，日期按构造对账
    if not trade_date or trade_date == date.today().isoformat():
        return None
    hist_rows = _records(cached_ak(
        'index_zh_a_hist',
        symbol=symbol[-6:],
        period='daily',
        start_date=(date.fromisoformat(trade_date) - timedelta(days=10)).isoformat().replace('-', ''),
        end_date=trade_date.replace('-', ''),
        timeout=20,
        retries=3,
    ))
    hist_row = next(
        (row for row in reversed(hist_rows) if row_compact_date(row) == trade_date.replace('-', '')),
        None,
    )
    return row_amount(hist_row) if hist_row else None


def _index_amount_sina_spot(symbol: str, trade_date: str | None, tail_date: str | None) -> Mapping[str, Any] | None:
    # 当日实时列表补额：旧语义要求尾行恰为所需交易日，顺带回写 close/日期
    if not trade_date or trade_date != date.today().isoformat() or tail_date != trade_date:
        return None
    spot_rows = _records(cached_ak('stock_zh_index_spot_sina', retries=2))
    spot = next((row for row in spot_rows if str(row.get('代码')) == symbol), None)
    if spot is None:
        return None
    spot_date = date.fromisoformat(trade_date)
    patch: dict[str, Any] = {'amount': spot.get('成交额'), '成交额': spot.get('成交额'), '日期': spot_date, 'date': spot_date}
    if spot.get('最新价'):
        patch['close'] = spot.get('最新价')
    return patch


def _index_amount_tencent_realtime(symbol: str, trade_date: str | None, tail_date: str | None) -> Mapping[str, Any] | None:
    # 腾讯实时串（qt）在非交易日返回最近交易日的全天数据：fields[30]=日期可与所需
    # 交易日对账后直接补成交额，不限于"当天跑"场景——周末/次日补跑 plan 同样受益
    quote = _tencent_realtime_quote(symbol)
    if quote is None or not trade_date or str(quote.get('date') or '') != str(trade_date):
        return None
    patch: dict[str, Any] = {'amount': quote['amount'], '成交额': quote['amount']}
    if quote.get('close'):
        patch['close'] = quote['close']
    return patch


def _index_rows_spot_synth(rows: list[dict[str, Any]], symbol: str, trade_date: str | None, limit: int) -> list[dict[str, Any]]:
    # 当日尾行缺失（K 线未及当日）时，用新浪指数实时行合成尾行
    if not trade_date or trade_date != date.today().isoformat():
        return rows
    if rows and (row_date(rows[-1]) or '') >= trade_date:
        return rows
    spot_rows = _records(cached_ak('stock_zh_index_spot_sina'))
    spot = next((row for row in spot_rows if str(row.get('代码')) == symbol), None)
    if spot is None:
        return rows
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
    if rows and row_compact_date(rows[-1]) == trade_date.replace('-', ''):
        rows[-1] = spot_row
    else:
        rows.append(spot_row)
    return _tail_slice(rows, limit)


_INDEX_ROW_FETCHERS = (
    RowFetcher(name='tencent_kline', fetch=_index_rows_tencent),
    RowFetcher(name='em_index_daily', fetch=_index_rows_em_hist),
    RowFetcher(name='sina_index_daily', fetch=_index_rows_sina_daily, fallback_only=True),
)
_INDEX_AMOUNT_FETCHERS = (
    AmountFetcher(name='overrides', fetch=_index_amount_overrides),
    AmountFetcher(name='em_index_hist', fetch=_index_amount_em_hist),
    AmountFetcher(name='sina_index_spot', fetch=_index_amount_sina_spot),
    AmountFetcher(name='tencent_realtime', fetch=_index_amount_tencent_realtime),
)
_INDEX_ROW_COMPLETIONS = (
    RowCompletion(name='sina_index_spot_row', apply=_index_rows_spot_synth),
)


def index_kline(symbol: str, trade_date: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    '''指数日 K。兜底链与补齐语义由 _INDEX_* 声明（resolvers.resolve_kline 统一执行）：
    腾讯快速路径 → em/新浪行兜底 → 人工兜底/em 历史/新浪实时/腾讯实时串补额 →
    首选源重取 → 当日实时行合成。'''
    result = resolve_kline(
        _INDEX_ROW_FETCHERS,
        _INDEX_AMOUNT_FETCHERS,
        _INDEX_ROW_COMPLETIONS,
        symbol=symbol,
        trade_date=trade_date,
        limit=limit,
        inclusive=True,
    )
    return result.rows


_SECTOR_ALIASES = {
    '3C设备': ('industry', '消费电子零部件及组装'),
    '光模块CPO': ('concept', 'CPO概念'),
    '存储': ('concept', '存储芯片'),
    '算力硬件': ('concept', '算力概念'),
    '先进封装': ('concept', '先进封装'),
    '量子科技': ('concept', '量子科技'),
    '新能源车': ('concept', '新能源车'),
}


def _sector_window(trade_date: str | None) -> tuple[str, str]:
    end = date.fromisoformat(trade_date) if trade_date else date.today()
    start = end - timedelta(days=180)
    return start.isoformat().replace('-', ''), end.isoformat().replace('-', '')


def _sector_rows_alias(symbol: str, trade_date: str | None, limit: int) -> Any:
    if symbol not in _SECTOR_ALIASES:
        return []
    source, board = _SECTOR_ALIASES[symbol]
    name = 'stock_board_industry_hist_em' if source == 'industry' else 'stock_board_concept_hist_em'
    start, end = _sector_window(trade_date)
    return cached_ak(name, symbol=board, start_date=start, end_date=end)


def _sector_rows_industry(symbol: str, trade_date: str | None, limit: int) -> Any:
    start, end = _sector_window(trade_date)
    return cached_ak(
        'stock_board_industry_hist_em',
        symbol=symbol,
        start_date=start,
        end_date=end,
    )


def _sector_rows_concept(symbol: str, trade_date: str | None, limit: int) -> Any:
    start, end = _sector_window(trade_date)
    return cached_ak(
        'stock_board_concept_hist_em',
        symbol=symbol,
        start_date=start,
        end_date=end,
    )


_SECTOR_ROW_FETCHERS = (
    RowFetcher(name='em_board_alias', fetch=_sector_rows_alias),
    RowFetcher(name='em_board_industry', fetch=_sector_rows_industry),
    RowFetcher(name='em_board_concept', fetch=_sector_rows_concept),
)


def sector_kline(symbol: str, limit: int = 60, trade_date: str | None = None) -> list[dict[str, Any]]:
    '''板块日 K：别名板块 → 行业板原名 → 概念板原名；无行数门槛、缺额即缺额、
    从不重取（东财独源，缺就缺）。'''
    result = resolve_kline(
        _SECTOR_ROW_FETCHERS,
        [],
        [],
        symbol=symbol,
        trade_date=trade_date,
        limit=limit,
        inclusive=True,
        require_amount=False,
        min_rows=1,
        last_resort='none',
    )
    return result.rows


def auction_snapshot(symbol: str, trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_zh_a_hist_min_em', symbol=symbol, period='1', adjust='')
    return [row for row in _tail(rows, trade_date, 1)]


def lhb_rows(trade_date: str) -> list[dict[str, Any]]:
    rows, _ = lhb_rows_checked(trade_date)
    return rows


def lhb_rows_checked(trade_date: str) -> tuple[list[dict[str, Any]], bool]:
    compact_date = trade_date.replace('-', '')
    rows = cached_ak('stock_lhb_detail_em', start_date=compact_date, end_date=compact_date)
    if rows is not None:
        return _tail(rows, trade_date, 100), True
    rows = cached_ak('stock_lhb_detail_daily_sina', date=compact_date)
    if rows is not None:
        return _records(rows)[-100:], True
    return [], False


def margin_rows(trade_date: str) -> list[dict[str, Any]]:
    rows = cached_ak('stock_margin_sse', date=trade_date)
    return _tail(rows, trade_date, 100)


def money_flow(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = cached_ak('stock_individual_fund_flow', stock=symbol, market='sh' if symbol.startswith('6') else 'sz')
    return _tail(rows, None, limit)


def stock_spot_rows() -> list[dict[str, Any]]:
    records = _records(cached_ak('stock_zh_a_spot_em'))
    if not records:
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


def stock_spot_quote(symbol: str) -> dict[str, Any]:
    rows = _records(cached_ak('stock_bid_ask_em', symbol=symbol))
    values = {
        str(row.get('item')): row.get('value')
        for row in rows
        if row.get('item') is not None
    }
    price = values.get('最新')
    if price is None:
        return {}
    return {'symbol': symbol, 'name': '', 'price': price}


def stock_spot_quotes(symbols: Sequence[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    result = []
    for parts in _tencent_quote_parts(symbols):
        symbol = str(parts[2] or '')
        try:
            price = float(parts[3])
        except (IndexError, TypeError, ValueError):
            continue
        if re.fullmatch(r'\d{6}', symbol) and price > 0:
            result.append({
                'symbol': symbol,
                'name': str(parts[1] or ''),
                'price': price,
            })
    return result


def stock_active_deltas(symbols: Sequence[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for index in range(0, len(symbols), 60):
        chunk = [str(symbol) for symbol in symbols[index:index + 60] if symbol]
        if not chunk:
            continue
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    _TENCENT_QUOTE_URL + ','.join(_tencent_symbol(symbol) for symbol in chunk),
                    headers={'User-Agent': 'Mozilla/5.0'},
                    timeout=8,
                )
            response.raise_for_status()
            response.encoding = 'gbk'
            for line in response.text.strip().split(';'):
                if '=' not in line:
                    continue
                payload = line.split('=', 1)[1].strip().strip('"')
                parts = payload.split('~')
                if len(parts) <= 8:
                    continue
                symbol = str(parts[2] or '')
                if not re.fullmatch(r'\d{6}', symbol):
                    continue
                raw_volume = float(parts[6] or 0)
                amount = float(parts[37] or 0) * 10000 if len(parts) > 37 else 0
                reference_price = float(parts[4] or parts[3] or 0)
                volume_factor = 1.0
                if raw_volume > 0 and reference_price > 0 and amount > 0:
                    volume_factor = amount / (raw_volume * reference_price)
                outer_volume = float(parts[7] or 0) * volume_factor
                inner_volume = float(parts[8] or 0) * volume_factor
                if outer_volume > 0 or inner_volume > 0:
                    result[symbol] = round(outer_volume - inner_volume, 6)
        except Exception as exc:
            logger.warning('tencent active delta failed: %s', exc)
    return result


def _tencent_quote_parts(symbols: Sequence[str]) -> list[list[str]]:
    parts: list[list[str]] = []
    for index in range(0, len(symbols), 60):
        chunk = [str(symbol) for symbol in symbols[index:index + 60] if symbol]
        if not chunk:
            continue
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    _TENCENT_QUOTE_URL + ','.join(_tencent_symbol(symbol) for symbol in chunk),
                    headers={'User-Agent': 'Mozilla/5.0'},
                    timeout=8,
                )
            response.raise_for_status()
            response.encoding = 'gbk'
            for line in response.text.strip().split(';'):
                if '=' not in line:
                    continue
                payload = line.split('=', 1)[1].strip().strip(chr(34))
                row_parts = payload.split('~')
                if len(row_parts) > 3:
                    parts.append(row_parts)
        except Exception as exc:
            logger.warning('tencent quote batch failed: %s', exc)
    return parts


def _eastmoney_index_kline_direct(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    '''东财日 K 直连兜底：akshare 的 em 封装被网络环境间歇拦截时，轮询 CDN 镜像域名直取。
    fields2 映射：f51=日期 f52=开盘 f53=收盘 f54=最高 f55=最低 f56=成交量 f57=成交额(元)。'''
    secid = ('1.' if symbol.startswith(('sh', '6')) else '0.') + symbol[-6:]
    for host in _EASTMONEY_KLINE_HOSTS:
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    f'https://{host}/api/qt/stock/kline/get',
                    params={
                        'secid': secid,
                        'fields1': 'f1,f2,f3,f4,f5',
                        'fields2': 'f51,f52,f53,f54,f55,f56,f57',
                        'klt': '101',
                        'fqt': '0',
                        'beg': start.isoformat().replace('-', ''),
                        'end': end.isoformat().replace('-', ''),
                    },
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0',
                        'Referer': 'https://quote.eastmoney.com/',
                    },
                    timeout=8,
                )
            response.raise_for_status()
            klines = (response.json().get('data') or {}).get('klines') or []
        except (requests.RequestException, ValueError):
            continue
        records: list[dict[str, Any]] = []
        for line in klines:
            parts = str(line).split(',')
            if len(parts) < 7:
                continue
            try:
                amount = float(parts[6])
            except (TypeError, ValueError):
                continue
            try:
                records.append({
                    'date': parts[0],
                    'open': float(parts[1]),
                    'close': float(parts[2]),
                    'high': float(parts[3]),
                    'low': float(parts[4]),
                    'volume': float(parts[5]),
                    'amount': amount,
                    '成交额': amount,
                })
            except (TypeError, ValueError):
                continue
        if records:
            return records
    return []


def _records(rows: Any) -> list[dict[str, Any]]:
    return as_records(rows)


def _latest_amount(row: Mapping[str, Any]) -> Any:
    return row_amount(row)


def _tencent_symbol(symbol: str) -> str:
    if symbol.startswith(('sh', 'sz', 'bj')):
        return symbol
    prefix = 'sh' if symbol.startswith('6') else 'sz' if symbol.startswith(('0', '3')) else 'bj'
    return f'{prefix}{symbol}'


def _tencent_realtime_quote(symbol: str) -> dict[str, Any] | None:
    '''腾讯实时报价兜底：K 线源（6 字段）不带成交额，qt 实时串有——
    fields[30] 前 8 位为日期，fields[37] 为成交额（单位万元）。'''
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                f'{_TENCENT_QUOTE_URL}{_tencent_symbol(symbol)}',
                headers={'Referer': 'https://gu.qq.com/'},
                timeout=8,
            )
        response.raise_for_status()
        fields = response.text.split('~')
        raw_date = str(fields[30] or '')[:8]
        amount_wan = float(fields[37])
        if len(raw_date) != 8 or amount_wan <= 0:
            return None
        return {
            'date': f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}',
            'amount': amount_wan * 10_000,
            'close': float(fields[3]) if fields[3] else None,
        }
    except (requests.RequestException, IndexError, TypeError, ValueError):
        return None


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
        quote_date = str(qt[30]).split()[0].replace('-', '') if len(qt) > 30 else ''
        last_date = str(records[-1].get('date') or '').replace('-', '') if records else ''
        if records and len(qt) > 37 and quote_date == last_date:
            try:
                records[-1]['amount'] = float(qt[37]) * 10_000
            except (TypeError, ValueError):
                pass
        return records[-max(limit, 0):]
    except Exception as exc:
        logger.warning('tencent kline failed: %s: %s', symbol, exc)
        return []


def _tail(rows: Any, trade_date: str | None, limit: int, *, inclusive: bool = False) -> list[dict[str, Any]]:
    return trim_to_date(rows, trade_date, limit, inclusive=inclusive)
