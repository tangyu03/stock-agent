"""§8 对账闭合：生成/排除处置记录的结构化落点。

候选从"池内一行"到"报告对账行"要穿过三道流水（市场评估排除、计划几何拒绝、
信号生成排除），任何一道静默丢弃都会让对账闭合失效。本模块只做三件事：
记录（record）、合并（merge）、计数（rejection_counts）——不解释原因，
中文标签留在 notify/render.py。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

Rejection = dict[str, str]  # {'symbol': ..., 'reason': ...}


def record_rejection(
    rejections: list[Rejection] | None,
    symbol: str,
    reason: str,
) -> None:
    """结构化处置记录；rejections 为 None 表示调用方不要求对账（静默合法）。"""
    if rejections is not None:
        rejections.append({'symbol': symbol, 'reason': reason})


def merge_rejections(*streams: Sequence[Rejection] | None) -> list[Rejection]:
    """按流顺序合并处置记录，保持流内顺序——对账行依赖出现次序，不得去重排序。"""
    merged: list[Rejection] = []
    for stream in streams:
        merged.extend(stream or [])
    return merged


def rejection_counts(rejections: Iterable[Mapping[str, Any] | Any]) -> dict[str, int]:
    """按 reason 计数；非 Mapping 行（脏数据）计入 'unknown'，不抛异常。"""
    counts: dict[str, int] = {}
    for row in rejections:
        reason = str(row.get('reason') or 'unknown') if isinstance(row, Mapping) else 'unknown'
        counts[reason] = counts.get(reason, 0) + 1
    return counts
