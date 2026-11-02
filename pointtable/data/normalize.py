"""数据行归一化：各数据源的行列语义（date/日期/trade_date、amount/成交额）在此统一。

出口约定：
- 日期一律归一为 'YYYY-MM-DD' 字符串（row_date）或 'YYYYMMDD' 紧凑串（compact_date）；
- amount/close/volume 一律归一为正 float 或 None（缺失/非正都算缺失，宁缺勿假）；
- 不得在本层之外再写 row.get('date') or row.get('日期') 式的多键探测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

DATE_KEYS = ('trade_date', '日期', 'date')


def as_records(rows: Any) -> list[dict[str, Any]]:
    """DataFrame / dict 序列 → list[dict]，并镜像 date→日期 保证单侧键可用。"""
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


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return f'{text[:4]}-{text[4:6]}-{text[6:]}'
    return text[:10]


def row_date(row: Mapping[str, Any]) -> str | None:
    """行日期，归一为 'YYYY-MM-DD'；无日期返回 None。"""
    for key in DATE_KEYS:
        text = _date_text(row.get(key))
        if text:
            return text
    return None


def compact_date(text: str | None) -> str:
    return (text or '').replace('-', '')


def row_compact_date(row: Mapping[str, Any]) -> str:
    return compact_date(row_date(row))


def row_amount(row: Mapping[str, Any]) -> float | None:
    """成交额（元）：非正/缺失一律 None。"""
    amount = row.get('amount') or row.get('成交额')
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def row_float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue
        return value
    return None


def filter_through_date(
    records: Sequence[Mapping[str, Any]],
    trade_date: str | None,
    *,
    inclusive: bool = False,
) -> list[dict[str, Any]]:
    """按交易日过滤：inclusive=True 保留 <=trade_date（历史序列），False 只留 ==trade_date。"""
    if not trade_date:
        return [dict(row) for row in records]
    target = compact_date(trade_date)
    compare = str.__le__ if inclusive else str.__eq__
    return [
        dict(row) for row in records
        if compare(row_compact_date(row), target)
    ]


def tail(records: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    return list(records[-max(limit, 0):])


def trim_to_date(
    rows: Any,
    trade_date: str | None,
    limit: int,
    *,
    inclusive: bool = False,
) -> list[dict[str, Any]]:
    """归一 + 过滤 + 截尾，等价于旧 _tail。"""
    return tail(filter_through_date(as_records(rows), trade_date, inclusive=inclusive), limit)
