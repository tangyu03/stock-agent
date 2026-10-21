"""Overlay locked-looking shadow points onto the existing signal flow.

Phase 2 remains observability-first: point rows annotate buy/sell signals, but
they do not override existing exit rules.
"""
import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..db import get_connection
from .sector_playbook import build_scenario
from .point_decision import decide_from_points

logger = logging.getLogger(__name__)

GENERATED_BY = "shadow_v1"
SELL_ACTIONS = {"STOP_DELAY", "SELL_PART", "CLEAR"}
BUY_ACTIONS = {"ADD", "BUY_FIRST"}


def list_point_entries(trade_date: str) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM point_entry
               WHERE trade_date=? AND generated_by=?
               ORDER BY symbol, zone_low""",
            (trade_date, GENERATED_BY),
        ).fetchall()
    return [dict(row) for row in rows]


def _near_zone(price: float, row: Dict, tolerance: float = 0.0075) -> bool:
    if price <= 0:
        return False
    low = float(row.get("zone_low") or 0)
    high = float(row.get("zone_high") or 0)
    if low <= 0 or high <= low:
        return False
    margin = max(low, high) * tolerance
    return (low - margin) <= price <= (high + margin)


def _source_label(source: str) -> str:
    return {
        "MA5_TOMORROW": "明日MA5",
        "PRIOR_LOW": "前低支撑",
        "RESISTANCE": "前高压力",
        "PAIRED_Z": "策略止损",
        "PAIRED_W": "策略止盈",
    }.get(source, source)


def _format_row(row: Dict) -> str:
    return (
        f"点表[{_source_label(str(row.get('source') or ''))}] "
        f"{float(row.get('zone_low') or 0):.2f}-{float(row.get('zone_high') or 0):.2f}"
    )


def _append_unique(value: str, suffix: str) -> str:
    value = value or ""
    if not suffix or suffix in value:
        return value
    return f"{value} | {suffix}" if value else suffix


def _classify(
    signal_action: str, point_action: str
) -> Tuple[str, str]:
    if signal_action == "buy":
        if point_action in BUY_ACTIONS:
            return "MATCH", "买入区一致"
        if point_action in SELL_ACTIONS:
            return "CONFLICT", "买入撞减仓/破位区"
        return "REFERENCE", "买入参考区"
    if signal_action == "sell":
        if point_action in SELL_ACTIONS:
            return "MATCH", "卖出/减仓区一致"
        if point_action in BUY_ACTIONS:
            return "CONFLICT", "卖出撞加仓区"
        return "REFERENCE", "卖出参考区"
    return "REFERENCE", "参考区"


def _scenario_stale(signal: Dict, row: Dict) -> bool:
    planned_state = str(row.get("sector_state") or "")
    current_state = str(signal.get("sector_status") or "")
    if not planned_state or planned_state == "unknown" or not current_state:
        return False
    if signal.get("sector_change_pct") is not None:
        current_scenario = build_scenario(
            market_mode=str(row.get("market_mode") or "defend"),
            sector_state=current_state,
            sector_name=str(signal.get("sector_name") or ""),
            sector_change_pct=signal.get("sector_change_pct"),
            sector_rank=int(signal.get("sector_rank") or 0),
            sector_total=int(signal.get("sector_total") or 0),
        )
        planned_scenario = f"{planned_state}_{row.get('rotation_direction') or ''}"
        return str(current_scenario.get("scenario_id")) != planned_scenario
    return planned_state != current_state


def _overlay_signals(
    signals: List[Dict],
    action: str,
    point_rows: Dict[str, List[Dict]],
) -> Dict[str, int]:
    counts = {"match": 0, "conflict": 0, "reference": 0, "stale": 0}
    for signal in signals:
        code = str(signal.get("stock_code") or "")
        rows = point_rows.get(code) or []
        if not rows:
            continue
        signal_price = float(
            signal.get("current_price") or signal.get("trigger_price") or 0
        )
        point_action = decide_from_points(rows, signal_price)
        if point_action:
            signal["point_action"] = point_action.get("action")
            signal["point_reason"] = point_action.get("reason")
        stale_rows = [row for row in rows if _scenario_stale(signal, row)]
        if stale_rows:
            planned = "；".join(sorted({
                f"{row.get('sector_state')}_{row.get('rotation_direction')}"
                for row in stale_rows
            }))
            current = str(signal.get("sector_status") or "unknown")
            line = f"点表作废(SCENARIO_STALE): 预案{planned}→当前{current}"
            if action == "buy":
                signal["point_status"] = "STALE"
                signal["note"] = _append_unique(str(signal.get("note") or ""), line)
            else:
                signal["point_status"] = "STALE"
                signal["reason"] = _append_unique(str(signal.get("reason") or ""), line)
            counts["stale"] += len(stale_rows)
            continue

        price = float(
            signal.get("trigger_price")
            or signal.get("current_price")
            or 0
        )
        matched = [row for row in rows if _near_zone(price, row)]
        if not matched:
            continue
        notes = []
        for row in matched:
            status, label = _classify(action, str(row.get("action") or ""))
            counts[status.lower()] += 1
            prefix = "点表一致" if status == "MATCH" else (
                "点表冲突" if status == "CONFLICT" else "点表参考"
            )
            notes.append(f"{prefix}: {_format_row(row)}({label})")
        line = "；".join(notes)
        if action == "buy":
            signal["point_status"] = next(
                (
                    status
                    for status in ("CONFLICT", "MATCH", "REFERENCE")
                    if any(
                        _classify(action, str(row.get("action") or ""))[0] == status
                        for row in matched
                    )
                ),
                "REFERENCE",
            )
            signal["note"] = _append_unique(str(signal.get("note") or ""), line)
        else:
            signal["point_status"] = next(
                (
                    status
                    for status in ("CONFLICT", "MATCH", "REFERENCE")
                    if any(
                        _classify(action, str(row.get("action") or ""))[0] == status
                        for row in matched
                    )
                ),
                "REFERENCE",
            )
            signal["reason"] = _append_unique(str(signal.get("reason") or ""), line)
    return counts


def apply_point_table_overlay(
    entries: List[Dict],
    exits: List[Dict],
    trade_date: str,
) -> Dict[str, object]:
    """Annotate current buy/sell signals with point-table context."""
    try:
        rows = list_point_entries(trade_date)
    except Exception as exc:
        logger.warning("点表覆盖失败: %s", exc)
        rows = []

    by_symbol: Dict[str, List[Dict]] = {}
    for row in rows:
        code = str(row.get("symbol") or "")
        if code:
            by_symbol.setdefault(code, []).append(row)

    result: Dict[str, object] = {
        "trade_date": trade_date,
        "rows": len(rows),
        "matched": 0,
        "conflicts": 0,
        "references": 0,
        "stale": 0,
        "available": bool(rows),
    }
    if not rows:
        return result

    entry_counts = _overlay_signals(entries, "buy", by_symbol)
    exit_counts = _overlay_signals(exits, "sell", by_symbol)
    result["matched"] = int(entry_counts["match"] + exit_counts["match"])
    result["conflicts"] = int(entry_counts["conflict"] + exit_counts["conflict"])
    result["references"] = int(entry_counts["reference"] + exit_counts["reference"])
    result["stale"] = int(entry_counts["stale"] + exit_counts["stale"])
    if result["stale"]:
        logger.warning(
            "点表状态过期 %d 条；板块剧本已变化，点位不应继续执行",
            result["stale"],
        )
    if result["conflicts"]:
        logger.warning(
            "点表冲突 %d 条（buy=%d, sell=%d）；第一阶段仅提示，不覆盖既有闸门",
            result["conflicts"], entry_counts["conflict"], exit_counts["conflict"],
        )
    return result
