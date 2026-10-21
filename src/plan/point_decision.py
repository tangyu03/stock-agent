"""Independent point-table trade decisions.

This module deliberately knows nothing about sector state.  It only compares a
stock's current price with its own precomputed MA5/support/resistance zones.
"""
import logging
from typing import Dict, Iterable, List, Optional, Sequence

from ..db import get_connection

logger = logging.getLogger(__name__)

GENERATED_BY = "shadow_v1"
SELL_ACTIONS = {"SELL", "STOP_DELAY", "SELL_PART", "CLEAR"}
BUY_ACTIONS = {"BUY", "ADD", "BUY_FIRST"}


def _near_zone(price: float, row: Dict, tolerance: float = 0.0075) -> bool:
    if price <= 0:
        return False
    low = float(row.get("zone_low") or 0)
    high = float(row.get("zone_high") or 0)
    if low <= 0 or high <= low:
        return False
    margin = max(low, high) * tolerance
    return (low - margin) <= price <= (high + margin)


def _distance(price: float, row: Dict) -> float:
    low = float(row.get("zone_low") or 0)
    high = float(row.get("zone_high") or 0)
    if low <= price <= high:
        return 0.0
    return min(abs(price - low), abs(price - high))


def _normalized_action(row: Dict) -> str:
    action = str(row.get("action") or "").upper()
    if action in BUY_ACTIONS:
        return "BUY"
    if action in SELL_ACTIONS:
        return "SELL"
    return "WATCH"


def _decision_from_row(row: Dict, price: float) -> Dict:
    action = _normalized_action(row)
    source = str(row.get("source") or "")
    zone_low = float(row.get("zone_low") or 0)
    zone_high = float(row.get("zone_high") or 0)
    return {
        "action": action,
        "source": source,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "action_size": float(row.get("action_size") or 0),
        "reverse_action": str(row.get("reverse_action") or ""),
        "invalid_if": str(row.get("invalid_if") or ""),
        "point_id": str(row.get("id") or ""),
        "reason": (
            f"价格{price:.2f}进入{source}区间{zone_low:.2f}-{zone_high:.2f}"
        ),
    }


def decide_from_points(rows: Sequence[Dict], price: float) -> Optional[Dict]:
    """Decide BUY/SELL/WATCH from a stock's own point zones only."""
    if price <= 0 or not rows:
        return None
    near = [row for row in rows if _near_zone(price, row)]
    if not near:
        return {
            "action": "WATCH",
            "source": "",
            "zone_low": 0.0,
            "zone_high": 0.0,
            "reason": "未触及点位",
        }
    nearest = min(near, key=lambda row: _distance(price, row))
    action = _normalized_action(nearest)
    if action == "WATCH":
        return {
            "action": action,
            "source": str(nearest.get("source") or ""),
            "zone_low": float(nearest.get("zone_low") or 0),
            "zone_high": float(nearest.get("zone_high") or 0),
            "reason": "点位动作未定义",
        }
    return _decision_from_row(nearest, price)


def decide_rows_from_points(rows: Sequence[Dict], price: float) -> List[Dict]:
    """Return every touched point row; each row preserves its own action."""
    if price <= 0:
        return []
    return [_decision_from_row(row, price) for row in rows if _near_zone(price, row)]


def _prices_from_signals(signal_groups: Iterable[List[Dict]]) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    for signals in signal_groups:
        for signal in signals or []:
            code = str(signal.get("stock_code") or "")
            price = float(signal.get("current_price") or signal.get("trigger_price") or 0)
            if code and price > 0:
                prices.setdefault(code, price)
    return prices


def build_point_decisions(
    entries: List[Dict],
    exits: List[Dict],
    observations: Optional[List[Dict]] = None,
    trade_date: str = "",
) -> List[Dict]:
    """Build a per-stock decision list from the current price and point table."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM point_entry
                   WHERE trade_date=? AND generated_by=?
                   ORDER BY symbol, zone_low""",
                (trade_date, GENERATED_BY),
            ).fetchall()
        point_rows = [dict(row) for row in rows]
    except Exception as exc:
        logger.warning("点表决策读取失败: %s", exc)
        point_rows = []

    by_symbol: Dict[str, List[Dict]] = {}
    for row in point_rows:
        code = str(row.get("symbol") or "")
        if code:
            by_symbol.setdefault(code, []).append(row)
    if not by_symbol:
        return []

    prices = _prices_from_signals([entries, exits, observations or []])
    results = []
    for code, rows_for_code in by_symbol.items():
        decisions = decide_rows_from_points(rows_for_code, prices.get(code, 0))
        if not decisions:
            continue
        first = rows_for_code[0]
        for decision in decisions:
            results.append({
                "stock_code": code,
                "stock_name": str(first.get("stock_name") or code),
                **decision,
            })
    return results

