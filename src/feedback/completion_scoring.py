# -*- coding: utf-8 -*-
"""P2-16 后验自动记分：完结事件自动记分并按策略/分型/位置三维累计。

记分规则（完结原因两级枚举的一级归属）：
  - sig_target      -> 命中 +1
  - sig_stop        -> 失败 -1
  - chase_abandon   -> 方向对节奏错 +0.5
  - invalidated     -> 原因含"止损/跌破"为失败 -1，否则未验证 0
  - time_exit       -> 未验证 0（到期未触目标/止损，信号口径平）
  - expired         -> 未验证 0（回踩买点有效期超期作废）

三维：策略(entry_type)/分型(entry_snapshot.pattern)/位置(entry_snapshot.position)，
缺失维度归"未记录"。累计样本 >= 10 时输出偏差方向（命中率显著低于整体的维度）。
"""
import json
from typing import Dict, List, Optional, Tuple

from ..db import get_conn

TERMINAL_STATUSES = (
    "invalidated", "expired", "chase_abandon",
    "sig_target", "sig_stop", "time_exit",
)

# 报告尾部区块标题：审计关键词"累计分型命中率"
BLOCK_TITLE = "📊 累计分型命中率（完结样本）"

_MIN_SAMPLES_FOR_DEVIATION = 10
_DEVIATION_THRESHOLD_PP = 10.0  # 命中率低于整体 10 个百分点视为偏差


def score_completed_event(
    status: str,
    reason: str = "",
) -> Tuple[float, str]:
    """按完结状态给出 (得分, 一级标签)。纯函数，可单测。"""
    status = str(status or "")
    reason = str(reason or "")
    if status == "sig_target":
        return 1.0, "命中"
    if status == "sig_stop":
        return -1.0, "失败"
    if status == "chase_abandon":
        return 0.5, "方向对节奏错"
    if status == "invalidated":
        if "止损" in reason or "跌破" in reason:
            return -1.0, "失败"
        return 0.0, "未验证"
    if status == "time_exit":
        return 0.0, "未验证"
    if status == "expired":
        return 0.0, "未验证"
    return 0.0, "未验证"


def _snapshot_field(entry_snapshot, key: str) -> str:
    """从 entry_snapshot JSON 提取分型/位置；缺失或解析失败归 '未记录'。"""
    try:
        data = json.loads(entry_snapshot) if isinstance(entry_snapshot, str) else (entry_snapshot or {})
        value = data.get(key)
        return str(value).strip() if value else ""
    except Exception:
        return ""


def load_completed_events() -> List[Dict]:
    """读全部完结事件（含 entry_snapshot 与 invalid_reason）。"""
    try:
        from ..analyzers.signal_lifecycle import _ensure_tables
        with get_conn() as conn:
            cursor = conn.cursor()
            _ensure_tables(cursor)
            placeholders = ",".join("?" * len(TERMINAL_STATUSES))
            cursor.execute(
                f"SELECT * FROM signal_events WHERE status IN ({placeholders})",
                TERMINAL_STATUSES,
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception:
        return []


def _event_dimensions(event: Dict) -> Dict:
    strategy = str(event.get("entry_type") or "").strip() or "未记录"
    pattern = _snapshot_field(event.get("entry_snapshot"), "pattern") or "未记录"
    position = _snapshot_field(event.get("entry_snapshot"), "position") or "未记录"
    return {"策略": strategy, "分型": pattern, "位置": position}


def aggregate_completion_scores(events: List[Dict]) -> Dict:
    """按策略/分型/位置三维累计。返回 {dimension: {bucket: stats}}。"""
    dims = {"策略": {}, "分型": {}, "位置": {}}
    for event in events:
        status = str(event.get("status") or "")
        reason = str(event.get("invalid_reason") or "")
        score, label = score_completed_event(status, reason)
        dim = _event_dimensions(event)
        for key, bucket in dim.items():
            stats = dims[key].setdefault(bucket, {
                "n": 0, "score": 0.0,
                "命中": 0, "方向对节奏错": 0, "未验证": 0, "失败": 0,
            })
            stats["n"] += 1
            stats["score"] += score
            stats[label] += 1
    total = {
        "n": len(events),
        "score": sum(
            score_completed_event(str(e.get("status") or ""), str(e.get("invalid_reason") or ""))[0]
            for e in events
        ),
        "命中": 0,
        "方向对节奏错": 0,
        "未验证": 0,
        "失败": 0,
    }
    for e in events:
        _, label = score_completed_event(
            str(e.get("status") or ""), str(e.get("invalid_reason") or "")
        )
        total[label] += 1
    return {"total": total, "dims": dims}


def _hit_rate(stats: Dict) -> Optional[float]:
    decisive = stats["命中"] + stats["失败"]
    if decisive <= 0:
        return None
    return stats["命中"] / decisive * 100.0


def deviation_directions(agg: Dict, events: List[Dict]) -> List[str]:
    """累计样本 >= 10 时输出偏差方向：命中率显著低于整体的维度。"""
    total_n = agg["total"]["n"]
    if total_n < _MIN_SAMPLES_FOR_DEVIATION:
        return []
    overall = _hit_rate(agg["total"])
    if overall is None:
        return []
    lines: List[str] = []
    for key, buckets in agg["dims"].items():
        for bucket, stats in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
            if stats["n"] < 3:
                continue
            rate = _hit_rate(stats)
            if rate is None:
                continue
            gap = overall - rate
            if gap >= _DEVIATION_THRESHOLD_PP:
                lines.append(
                    f"{key}「{bucket}」命中率{rate:.0f}% 低于整体{overall:.0f}% "
                    f"{gap:.0f}个百分点"
                )
    return lines


def _bucket_line(bucket: str, stats: Dict) -> str:
    rate = _hit_rate(stats)
    rate_text = f"命中率{rate:.0f}%" if rate is not None else "无胜负样本"
    return (
        f"{bucket}: N={stats['n']} {rate_text} "
        f"命中{stats['命中']}/节奏错{stats['方向对节奏错']}/"
        f"未验证{stats['未验证']}/失败{stats['失败']} 得分{stats['score']:+.1f}"
    )


def render_completion_score_block(events: Optional[List[Dict]] = None) -> str:
    """渲染报告尾部累计分型命中率区块。events 缺省时读库（离线安全）。"""
    if events is None:
        events = load_completed_events()
    if not events:
        return (
            f"{BLOCK_TITLE}\n"
            "  暂无完结事件，无命中率统计（后验记分待样本积累）"
        )
    agg = aggregate_completion_scores(events)
    total = agg["total"]
    hits = sum(1 for e in events if score_completed_event(
        str(e.get("status") or ""), str(e.get("invalid_reason") or ""))[1] == "命中")
    fails = sum(1 for e in events if score_completed_event(
        str(e.get("status") or ""), str(e.get("invalid_reason") or ""))[1] == "失败")
    overall = hits / (hits + fails) * 100.0 if (hits + fails) else None
    overall_text = f"整体命中率{overall:.0f}%" if overall is not None else "整体无胜负样本"

    lines = [
        BLOCK_TITLE,
        f"  总样本{total['n']} 得分{total['score']:+.1f} {overall_text}",
    ]
    for key in ("策略", "分型", "位置"):
        buckets = agg["dims"].get(key) or {}
        if buckets:
            lines.append(f"  按{key}: " + " | ".join(
                _bucket_line(b, s) for b, s in sorted(buckets.items(), key=lambda kv: -kv[1]["n"])
            ))
    for line in deviation_directions(agg, events):
        lines.append(f"  ⚠偏差方向: {line}")
    return "\n".join(lines)
