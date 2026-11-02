"""stock_kline / sector_kline 迁移到 resolvers 引擎后的接线契约测试。

钉死三件事（全部离线，patch 模块级 _tencent_kline / cached_ak）：
1. 兜底链顺序与"非空即停"：腾讯 → em 历史 → 新浪日线；
2. last_resort='empty'：仅链整体为空才重取首选源，行数不足不重取；
3. sector 别名路由：别名→对应板块 em，非别名→行业板→概念板，从不重取。
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from pointtable.data import akshare


def _rows(*dates: str) -> list[dict]:
    return [{'date': day, 'open': 1.0, 'close': 2.0, 'volume': 3.0} for day in dates]


def _stub_ak(calls: list[tuple], effects: dict[str, list[dict]]):
    def cached_ak(name: str, **kwargs):
        calls.append((name, kwargs))
        return effects.get(name, [])
    return cached_ak


def test_stock_kline_quick_path_skips_fallbacks():
    calls: list[tuple] = []
    days = [f'2026-10-{day:02d}' for day in range(1, 31)]  # 30 行，行数足够
    tencent_calls: list[tuple] = []

    def tencent(symbol, start, end, limit):
        tencent_calls.append((symbol, start, end, limit))
        return _rows(*days)

    with patch.object(akshare, '_tencent_kline', tencent), \
            patch.object(akshare, 'cached_ak', _stub_ak(calls, {})):
        rows = akshare.stock_kline('600000', trade_date='2026-10-30', limit=60)
    assert len(rows) == 30
    assert tencent_calls and calls == []  # em/新浪没被触碰


def test_stock_kline_falls_back_to_em_then_sina_with_prefix():
    calls: list[tuple] = []
    effects = {
        'stock_zh_a_hist': [],
        'stock_zh_a_daily': _rows('2026-10-29', '2026-10-30'),
    }

    def tencent(symbol, start, end, limit):
        return []

    with patch.object(akshare, '_tencent_kline', tencent), \
            patch.object(akshare, 'cached_ak', _stub_ak(calls, effects)):
        rows = akshare.stock_kline('600000', trade_date='2026-10-30', limit=60)
    assert [row['date'] for row in rows] == ['2026-10-29', '2026-10-30']
    names = [name for name, _ in calls]
    assert names == ['stock_zh_a_hist', 'stock_zh_a_daily']
    # 新浪日线需要交易所前缀
    assert calls[1][1]['symbol'] == 'sh600000'
    assert calls[1][1]['adjust'] == 'qfq'


def test_stock_kline_sina_prefix_covers_sz_and_bj():
    for symbol, prefix in (('000001', 'sz'), ('300750', 'sz'), ('830000', 'bj')):
        calls: list[tuple] = []
        effects = {'stock_zh_a_daily': _rows('2026-10-30')}
        with patch.object(akshare, '_tencent_kline', lambda *a: []), \
                patch.object(akshare, 'cached_ak', _stub_ak(calls, effects)):
            rows = akshare.stock_kline(symbol, trade_date='2026-10-30', limit=5)
        assert rows and calls[-1][1]['symbol'] == f'{prefix}{symbol}'


def test_stock_kline_last_resort_refetch_only_when_chain_empty():
    # 链全空 → 腾讯重取一次（last_resort='empty'）
    calls: list[tuple] = []
    tencent_calls: list[tuple] = []

    def tencent(symbol, start, end, limit):
        tencent_calls.append(symbol)
        return []

    with patch.object(akshare, '_tencent_kline', tencent), \
            patch.object(akshare, 'cached_ak', _stub_ak(calls, {})):
        rows = akshare.stock_kline('600000', trade_date='2026-10-30', limit=60)
    assert rows == []
    assert len(tencent_calls) == 2  # 首发 + 空链重取
    assert [name for name, _ in calls] == ['stock_zh_a_hist', 'stock_zh_a_daily']

    # 链非空但行数不足 → 不重取（个股语义与指数不同）
    calls.clear()
    tencent_calls.clear()
    effects = {'stock_zh_a_hist': _rows('2026-10-30')}  # 仅 1 行
    with patch.object(akshare, '_tencent_kline', tencent), \
            patch.object(akshare, 'cached_ak', _stub_ak(calls, effects)):
        rows = akshare.stock_kline('600000', trade_date='2026-10-30', limit=60)
    assert len(tencent_calls) == 1  # 无二次重取
    assert len(rows) == 1


def test_stock_kline_does_not_require_amount():
    # 个股 K 线无成交额语义：尾行缺 amount 不触发兜底
    calls: list[tuple] = []
    days = [f'2026-10-{day:02d}' for day in range(1, 31)]
    with patch.object(akshare, '_tencent_kline', lambda *a: _rows(*days)), \
            patch.object(akshare, 'cached_ak', _stub_ak(calls, {})):
        rows = akshare.stock_kline('600000', trade_date='2026-10-30', limit=60)
    assert len(rows) == 30
    assert calls == []


def test_sector_kline_alias_routes_to_board_em():
    calls: list[tuple] = []
    effects = {'stock_board_concept_hist_em': _rows('2026-10-29', '2026-10-30')}
    with patch.object(akshare, 'cached_ak', _stub_ak(calls, effects)):
        rows = akshare.sector_kline('光模块CPO', trade_date='2026-10-30', limit=60)
    assert [row['date'] for row in rows] == ['2026-10-29', '2026-10-30']
    assert len(calls) == 1  # 命中即停
    name, kwargs = calls[0]
    assert name == 'stock_board_concept_hist_em'
    assert kwargs['symbol'] == 'CPO概念'


def test_sector_kline_industry_hit_skips_concept():
    calls: list[tuple] = []
    effects = {'stock_board_industry_hist_em': _rows('2026-10-30')}
    with patch.object(akshare, 'cached_ak', _stub_ak(calls, effects)):
        rows = akshare.sector_kline('半导体', trade_date='2026-10-30', limit=60)
    assert [row['date'] for row in rows] == ['2026-10-30']
    assert [name for name, _ in calls] == ['stock_board_industry_hist_em']


def test_sector_kline_falls_to_concept_then_stops():
    calls: list[tuple] = []
    # 别名板块（存储芯片）为空，原始名在概念板命中
    def stub(name: str, **kwargs):
        calls.append((name, kwargs.get('symbol')))
        if name == 'stock_board_concept_hist_em' and kwargs.get('symbol') == '存储':
            return _rows('2026-10-30')
        return []

    with patch.object(akshare, 'cached_ak', stub):
        rows = akshare.sector_kline('存储', trade_date='2026-10-30', limit=60)
    # '存储' 是别名（概念板），但别名命中为空后仍落到行业/概念原始名重试
    assert [row['date'] for row in rows] == ['2026-10-30']
    assert calls == [
        ('stock_board_concept_hist_em', '存储芯片'),  # 别名路由
        ('stock_board_industry_hist_em', '存储'),  # 原始名行业板
        ('stock_board_concept_hist_em', '存储'),  # 原始名概念板
    ]


def test_sector_kline_all_empty_returns_empty_no_refetch():
    calls: list[tuple] = []
    with patch.object(akshare, 'cached_ak', _stub_ak(calls, {})):
        rows = akshare.sector_kline('某板块', trade_date='2026-10-30', limit=60)
    assert rows == []
    assert len(calls) == 2  # 行业板 + 概念板，各一次，从不重取
