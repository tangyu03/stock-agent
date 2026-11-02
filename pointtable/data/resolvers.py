"""声明式兜底引擎：K 线/数值的兜底链在这里统一编排，各数据源只声明自己怎么取。

设计约束（承自 index_kline 多年演化出的行为，语义逐条保留）：
1. 首选源"行数足够且尾行带关键值"才快速返回，否则进入后续链；
2. 行兜底链按声明顺序取第一个非空者（整体替换，不做行级合并）；
3. 数值补齐（amount 等）只认"日期对账一致"的结果，日期不符宁可缺失；
4. 行数不足或关键值缺失时，用首选源重取一次作最后手段（其内嵌实时块可能已更新）；
5. 行完成（如当日实时行合成）在最后，能改行集但不参与对账语义。

引擎不关心具体数据源；数据源在 akshare.py 中以 fetcher 函数声明，测试可按名替换。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from .normalize import as_records, filter_through_date, row_amount, row_date, tail

logger = logging.getLogger(__name__)

MIN_USABLE_ROWS = 20


@dataclass(frozen=True)
class RowFetcher:
    """K 线行来源。fetch(symbol, trade_date, limit) -> 原始行集（DataFrame/序列/None）。

    fallback_only=True 表示只在现有行集为空或尾行早于所需交易日时才尝试
    （对应旧逻辑 needs_spot 才去取 sina 日线）。
    """
    name: str
    fetch: Callable[[str, str | None, int], Any]
    fallback_only: bool = False


@dataclass(frozen=True)
class AmountFetcher:
    """数值补齐来源。fetch(symbol, trade_date, tail_date) -> float | dict | None。

    返回 float 视为尾行成交额（元）；返回 dict 视为对尾行的字段补丁
    （至少含 'amount'，可带 'close'/'日期' 等）。实现者必须自带日期对账：
    结果不属于 trade_date 当日就返回 None，绝不张冠李戴；tail_date 是
    当前尾行日期，供"仅当尾行恰为所需交易日"类门控使用。
    """
    name: str
    fetch: Callable[[str, str | None, str | None], float | Mapping[str, Any] | None]


@dataclass(frozen=True)
class RowCompletion:
    """行集补全（最后阶段）。apply(rows, symbol, trade_date, limit) -> rows。"""
    name: str
    apply: Callable[[list[dict[str, Any]], str, str | None, int], list[dict[str, Any]]]


@dataclass
class KlineResult:
    rows: list[dict[str, Any]]
    row_source: str | None = None
    amount_source: str | None = None
    status: str = 'EMPTY'  # OK / AMOUNT_MISSING / INSUFFICIENT / EMPTY
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 'OK'


def _tail_amount(rows: list[dict[str, Any]]) -> float | None:
    return row_amount(rows[-1]) if rows else None


def resolve_kline(
    row_fetchers: Sequence[RowFetcher],
    amount_fetchers: Sequence[AmountFetcher],
    row_completions: Sequence[RowCompletion],
    *,
    symbol: str,
    trade_date: str | None,
    limit: int,
    inclusive: bool = True,
    min_rows: int = MIN_USABLE_ROWS,
    require_amount: bool = True,
    last_resort: str = 'failure',
) -> KlineResult:
    """按声明链解析 K 线。行过滤/归一/截尾由引擎统一做，fetcher 只管取原始数据。

    require_amount=False 用于无成交额语义的行集（个股/板块 K 线）：
    快速路径只看行数，不再因尾行缺 amount 触发。
    last_resort 控制首选源重取的触发条件（各 datum 的旧语义不同）：
    - 'failure'：行数不足或关键值缺失或为空都重取（指数 K 线，其内嵌实时块可能已更新）；
    - 'empty'：仅当兜底链整体为空时重取（个股 K 线）；
    - 'none'：从不重取（板块 K 线）。
    """
    usable = max(min(limit, min_rows), 1)
    result = KlineResult(rows=[])

    def fetch_rows(fetcher: RowFetcher) -> list[dict[str, Any]]:
        raw = fetcher.fetch(symbol, trade_date, limit)
        # fetcher 返回 DataFrame / 序列 / None（失败）皆可，引擎统一归一
        return tail(filter_through_date(as_records(raw), trade_date, inclusive=inclusive), limit)

    def enough(rows: list[dict[str, Any]]) -> bool:
        return len(rows) >= usable

    def metric_missing(rows: list[dict[str, Any]]) -> bool:
        return require_amount and _tail_amount(rows) is None

    # 1. 首选源快速路径：行数足够且尾行自带关键值
    primary = row_fetchers[0]
    rows = fetch_rows(primary)
    if enough(rows) and not metric_missing(rows):
        result.rows, result.row_source, result.status = rows, primary.name, 'OK'
        return result

    # 2. 行兜底链：第一个非空者整体替换；fallback_only 仅在空/尾行早于所需交易日时尝试
    for fetcher in row_fetchers[1:]:
        stale = bool(rows and trade_date and (row_date(rows[-1]) or '') < trade_date)
        if fetcher.fallback_only and rows and not stale:
            continue
        got = fetch_rows(fetcher)
        if got:
            rows = got
            result.row_source = fetcher.name
            break

    # 3. 数值补齐：逐个来源，日期对账由 fetcher 自保证
    if rows and _tail_amount(rows) is None:
        tail_date = row_date(rows[-1])
        for fetcher in amount_fetchers:
            patch = fetcher.fetch(symbol, trade_date, tail_date)
            if patch is None:
                continue
            if isinstance(patch, Mapping):
                if row_amount(patch) is None:
                    continue
                rows[-1].update(patch)
            else:
                rows[-1]['amount'] = float(patch)
                rows[-1]['成交额'] = float(patch)
            result.amount_source = fetcher.name
            break

    # 4. 最后手段：按 datum 语义决定是否用首选源重取
    if last_resort == 'failure' and (not rows or not enough(rows) or metric_missing(rows)):
        rows = fetch_rows(primary)
        result.row_source = primary.name
    elif last_resort == 'empty' and not rows:
        rows = fetch_rows(primary)
        result.row_source = primary.name

    # 5. 行完成：改行集，不参与对账语义
    for completion in row_completions:
        rows = completion.apply(rows, symbol, trade_date, limit)

    # 6. 状态与溯源
    result.rows = rows
    if not rows:
        result.status = 'EMPTY'
    elif not enough(rows):
        result.status = 'INSUFFICIENT'
    elif _tail_amount(rows) is None:
        result.status = 'AMOUNT_MISSING'
    else:
        result.status = 'OK'
    if result.status != 'OK':
        logger.warning(
            'kline resolve %s/%s: status=%s rows=%d row_source=%s amount_source=%s',
            symbol, trade_date, result.status, len(rows),
            result.row_source, result.amount_source,
        )
    return result
