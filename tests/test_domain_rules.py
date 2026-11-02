"""domain/reconciliation 契约测试：§8 对账闭合的结构化处置记录。

- record：rejections 为 None 是合法静默（调用方不要求对账）；
- merge：保持流顺序与重复项——对账行依赖出现次序，不得去重排序；
- counts：非 Mapping 脏数据计入 'unknown'，缺 reason 同样计 'unknown'，不抛异常。
"""

from __future__ import annotations

from pointtable.domain.reconciliation import (
    merge_rejections,
    record_rejection,
    rejection_counts,
)


def test_record_rejection_appends_and_silences():
    stream: list[dict[str, str]] = []
    record_rejection(stream, '600001', 'no_strategy')
    assert stream == [{'symbol': '600001', 'reason': 'no_strategy'}]
    record_rejection(None, '600002', 'stale_kline')  # 不要求对账 → 静默合法


def test_merge_rejections_preserves_order_and_duplicates():
    first = [{'symbol': '600001', 'reason': 'no_strategy'}]
    second = [
        {'symbol': '600001', 'reason': 'no_strategy'},  # 同票同因可重复出现
        {'symbol': '000002', 'reason': 'premise_failed'},
    ]
    merged = merge_rejections(first, None, second)
    assert merged == [
        {'symbol': '600001', 'reason': 'no_strategy'},
        {'symbol': '600001', 'reason': 'no_strategy'},
        {'symbol': '000002', 'reason': 'premise_failed'},
    ]


def test_rejection_counts_tolerates_dirty_rows():
    rows = [
        {'reason': 'no_strategy'},
        {'symbol': '000002'},  # 缺 reason
        'garbage',  # 非 Mapping
        {'reason': 'no_strategy'},
    ]
    assert rejection_counts(rows) == {'no_strategy': 2, 'unknown': 2}
    assert rejection_counts([]) == {}
