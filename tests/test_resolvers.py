"""resolvers.resolve_kline 契约测试：兜底链语义逐条钉死，全部离线（fetcher 用桩）。

语义清单（承自 index_kline 多年演化行为，见 resolvers.py 模块注释）：
1. 首选源行数足够且尾行带关键值 → 快速返回，不再触碰后续来源；
2. 行兜底链取第一个非空者并整体替换；fallback_only 仅在空/尾行早于所需日时尝试；
3. 数值补齐逐个尝试，fetcher 自带日期对账，返回 None 即跳过；
4. 行数不足或关键值缺失 → 首选源重取并整体替换；
5. 行完成最后执行，可改行集；
6. 状态标注 OK / AMOUNT_MISSING / INSUFFICIENT / EMPTY。
"""

from __future__ import annotations

from datetime import date, timedelta

from pointtable.data.resolvers import (
    AmountFetcher,
    RowCompletion,
    RowFetcher,
    resolve_kline,
)


def _rows(*dates: str, amount: float | None = None) -> list[dict]:
    return [
        {'date': day, 'close': 100.0 + index, 'volume': 1.0, **({'amount': amount} if amount else {})}
        for index, day in enumerate(dates)
    ]


def _fetcher(name: str, result, calls: list[str]):
    def fetch(symbol, trade_date, limit):
        calls.append(name)
        return result
    return RowFetcher(name=name, fetch=fetch)


def _amount_fetcher(name: str, result, calls: list[str]):
    def fetch(symbol, trade_date, tail_date):
        calls.append(name)
        return result
    return AmountFetcher(name=name, fetch=fetch)


def test_quick_path_does_not_touch_fallbacks():
    calls: list[str] = []
    primary = _fetcher('primary', _rows('2026-10-29', '2026-10-30', amount=764e8), calls)
    result = resolve_kline(
        [primary],
        [_amount_fetcher('overrides', 100e8, calls)],
        [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert result.status == 'OK'
    assert result.row_source == 'primary'
    assert result.amount_source is None  # 尾行自带成交额，无需补齐
    assert calls == ['primary']


def test_row_fallback_first_non_empty_replaces_wholesale():
    calls: list[str] = []
    primary = _fetcher('primary', _rows('2026-10-30'), calls)  # 行数不足
    em = _fetcher('em', _rows('2026-10-29', '2026-10-30', amount=1e8), calls)
    sina = _fetcher('sina', _rows('2026-10-30', amount=2e8), calls)
    result = resolve_kline(
        [primary, em, sina],
        [], [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert result.row_source == 'em'
    assert result.amount_source is None
    assert [row['date'] for row in result.rows] == ['2026-10-29', '2026-10-30']
    assert calls == ['primary', 'em']  # sina 不该被触碰


def test_fallback_only_skipped_when_fresh_tried_when_stale():
    calls: list[str] = []
    fallback = _fetcher('sina_daily', _rows('2026-10-29', '2026-10-30', amount=2e8), calls)
    # 快速路径成功（行数足够+尾行新鲜+带额）→ fallback_only 跳过
    fresh_primary = _fetcher('primary', _rows('2026-10-29', '2026-10-30', amount=1e8), calls)
    resolve_kline(
        [fresh_primary, fallback], [], [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert calls == ['primary']
    # 行数不足且尾行早于所需交易日 → fallback_only 尝试并整体替换
    calls.clear()
    stale_primary = _fetcher('primary', _rows('2026-10-23'), calls)
    result = resolve_kline(
        [stale_primary, fallback], [], [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert calls == ['primary', 'sina_daily']
    assert result.row_source == 'sina_daily'
    assert result.status == 'OK'


def test_amount_completion_is_date_gated_by_fetcher():
    calls: list[str] = []
    primary = _fetcher('primary', _rows('2026-10-29', '2026-10-30'), calls)  # 尾行无 amount
    gated = _amount_fetcher('date_gated', None, calls)  # 日期不符返回 None
    realtime = _amount_fetcher('realtime', {'amount': 764e8, 'close': 3599.02}, calls)
    result = resolve_kline(
        [primary], [gated, realtime], [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert result.status == 'OK'
    assert result.amount_source == 'realtime'
    assert result.rows[-1]['amount'] == 764e8
    assert result.rows[-1]['close'] == 3599.02


def test_last_resort_refetches_primary_when_amount_missing():
    calls: list[str] = []
    primary = _fetcher('primary', _rows('2026-10-29', '2026-10-30'), calls)
    result = resolve_kline(
        [primary], [_amount_fetcher('overrides', None, calls)], [],
        symbol='sh000001', trade_date='2026-10-30', limit=2,
    )
    assert calls == ['primary', 'overrides', 'primary']  # 缺额 → 首选源重取
    assert result.status == 'AMOUNT_MISSING'
    assert result.row_source == 'primary'


def test_row_completion_runs_after_last_resort():
    calls: list[str] = []

    def synth(rows, symbol, trade_date, limit):
        calls.append('synth')
        return rows + [{'date': trade_date, 'close': 1.0, 'amount': 1e8}]

    primary = _fetcher('primary', _rows('2026-10-23'), calls)  # 行数不足且过期
    result = resolve_kline(
        [primary],
        [_amount_fetcher('overrides', None, calls)],
        [RowCompletion(name='synth', apply=synth)],
        symbol='sh000001', trade_date=str(date.today()), limit=2,
    )
    # 顺序：primary → overrides → primary(重取) → synth
    assert calls == ['primary', 'overrides', 'primary', 'synth']
    assert result.rows[-1]['date'] == str(date.today())


def test_status_annotations():
    calls: list[str] = []
    result = resolve_kline(
        [_fetcher('primary', [], calls)], [], [],
        symbol='sh000001', trade_date='2026-10-30', limit=60,
    )
    assert result.status == 'EMPTY'
    result = resolve_kline(
        [_fetcher('primary', _rows('2026-10-30', amount=1e8), calls)], [], [],
        symbol='sh000001', trade_date='2026-10-30', limit=60,
    )
    assert result.status == 'INSUFFICIENT'  # 1 行 < min(60, 20)


def test_fetcher_returning_none_is_treated_as_empty():
    # 数据源失败（cached_ak 返回 None / DataFrame）由引擎统一归一，链继续
    calls: list[str] = []
    dead = _fetcher('dead', None, calls)
    alive = _fetcher('alive', _rows('2026-10-29', '2026-10-30', amount=1e8), calls)
    result = resolve_kline(
        [dead, alive], [], [],
        symbol='sz399106', trade_date='2026-10-30', limit=2,
    )
    assert result.row_source == 'alive'
    assert result.status == 'OK'
    assert calls == ['dead', 'alive']


def test_inclusive_filter_keeps_history_for_past_trade_date():
    calls: list[str] = []
    old_day = (date.today() - timedelta(days=30)).isoformat()
    older_day = (date.today() - timedelta(days=31)).isoformat()
    fresh_day = date.today().isoformat()
    primary = _fetcher('primary', _rows(older_day, old_day, fresh_day, amount=1e8), calls)
    result = resolve_kline(
        [primary], [], [],
        symbol='sh000001', trade_date=old_day, limit=2,
    )
    # inclusive=True：历史序列保留 <=trade_date，未来行被过滤
    assert [row['date'] for row in result.rows] == [older_day, old_day]
    assert result.status == 'OK'
