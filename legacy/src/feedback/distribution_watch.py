"""派发嫌疑生命周期（P0-5）：嫌疑→确认/解除 状态机 + 每日快照 + 转换日志。

审计要求：昨日嫌疑票今日必须出现在状态机任一终态或延续态，不得静默消失
（蘅东光 9-16→9-17 案例）。状态转换写入事件日志并可供后验记分。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..db import get_conn

logger = logging.getLogger(__name__)

STATE_NONE = "无"
STATE_SUSPECT = "嫌疑"
STATE_CONFIRMED = "确认"
STATE_RELEASED = "解除"

_STATE_LABELS = {
    STATE_NONE: "无",
    STATE_SUSPECT: "派发嫌疑",
    STATE_CONFIRMED: "派发确认",
    STATE_RELEASED: "嫌疑解除",
}

# 进入嫌疑的触发证据
ENTRY_REASONS = ("高位超买+顶背驰", "折价大宗")
# 确认规则（嫌疑→确认）
CONFIRM_REASONS = ("价格下行", "机构减持", "折价大宗", "主力净流出扩大")
# 自设解除条件（嫌疑→解除，与 volume_pattern 的派发嫌疑观察 confirms 对齐）
RELEASE_REASONS = ("RSI6回落70下方", "顶背驰消失", "缩量回踩不破MA5")

# 价格下行判定阈值（跌幅≥3%）
_PRICE_DOWN_THRESHOLD = -3.0


def _ensure_table(cursor) -> None:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS distribution_watch (
        stock_code TEXT PRIMARY KEY,
        stock_name TEXT,
        state TEXT NOT NULL,
        reason TEXT DEFAULT '',
        rule_entry TEXT DEFAULT '',
        as_of TEXT NOT NULL,
        entered_at TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS distribution_watch_snapshot (
        as_of TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        state TEXT NOT NULL,
        reason TEXT DEFAULT '',
        rule_entry TEXT DEFAULT '',
        PRIMARY KEY (as_of, stock_code)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS distribution_watch_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        as_of TEXT NOT NULL,
        reason TEXT DEFAULT '',
        rule_entry TEXT DEFAULT '',
        trigger_data TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


def _safe_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _price_down(price_change_pct: Optional[float]) -> bool:
    pct = _safe_float(price_change_pct)
    return pct is not None and pct <= _PRICE_DOWN_THRESHOLD


def _break_ma5(close, ma5) -> bool:
    c, m = _safe_float(close), _safe_float(ma5)
    return c is not None and m is not None and c < m


def evaluate_distribution_state(
    state: Optional[str],
    *,
    rsi6=None,
    top_divergence: bool = False,
    price_change_pct: Optional[float] = None,
    close=None,
    ma5=None,
    volume_ratio: Optional[float] = None,
    main_force_net: Optional[float] = None,
    main_force_prev: Optional[float] = None,
    block_trade_trigger: bool = False,
    institutional_reduce: bool = False,
) -> Dict[str, Any]:
    """推进派发状态机一步。

    优先级：确认证据（价格下行/折价大宗/主力净流出扩大/机构减持）优先于
    解除证据——价格大跌本身就是派发兑现，不能让滞后指标 RSI 回落误判为解除。

    Returns:
        {"state", "reason", "rule", "changed", "from_state"}
    """
    current = state or STATE_NONE
    if current not in _STATE_LABELS:
        current = STATE_NONE

    # 终态可被新证据重新拉回嫌疑（新的派发周期）
    if current in (STATE_CONFIRMED, STATE_RELEASED):
        if block_trade_trigger or (top_divergence and (_safe_float(rsi6) or 0) > 70):
            return {
                "state": STATE_SUSPECT, "from_state": current,
                "reason": "折价大宗" if block_trade_trigger else ENTRY_REASONS[0],
                "rule": "P0-5重新进入嫌疑",
                "changed": True,
            }
        return {"state": current, "from_state": current,
                "reason": "", "rule": "终态保持", "changed": False}

    # 进入嫌疑（从无）
    if current == STATE_NONE:
        if block_trade_trigger:
            return {"state": STATE_SUSPECT, "from_state": STATE_NONE,
                    "reason": "折价大宗", "rule": "P0-6折价大宗触发",
                    "changed": True}
        rsi = _safe_float(rsi6)
        if top_divergence and rsi is not None and rsi > 70:
            return {"state": STATE_SUSPECT, "from_state": STATE_NONE,
                    "reason": ENTRY_REASONS[0], "rule": "派发嫌疑观察",
                    "changed": True}
        return {"state": STATE_NONE, "from_state": STATE_NONE,
                "reason": "", "rule": "无派发证据", "changed": False}

    # 当前是嫌疑
    assert current == STATE_SUSPECT
    # 确认证据（优先级高于解除）
    if block_trade_trigger:
        return {"state": STATE_CONFIRMED, "from_state": STATE_SUSPECT,
                "reason": "折价大宗", "rule": "P0-6折价大宗确认",
                "changed": True}
    if institutional_reduce:
        return {"state": STATE_CONFIRMED, "from_state": STATE_SUSPECT,
                "reason": "机构减持", "rule": "派发确认-机构减持",
                "changed": True}
    net, prev = _safe_float(main_force_net), _safe_float(main_force_prev)
    if net is not None and net < 0 and (prev is None or net <= prev):
        return {"state": STATE_CONFIRMED, "from_state": STATE_SUSPECT,
                "reason": "主力净流出扩大", "rule": "派发确认-主力净流出扩大",
                "changed": True}
    if _price_down(price_change_pct) or _break_ma5(close, ma5):
        return {"state": STATE_CONFIRMED, "from_state": STATE_SUSPECT,
                "reason": "价格下行", "rule": "派发确认-价格下行",
                "changed": True}
    # 解除证据
    rsi = _safe_float(rsi6)
    if rsi is not None and rsi < 70:
        return {"state": STATE_RELEASED, "from_state": STATE_SUSPECT,
                "reason": RELEASE_REASONS[0], "rule": "解除-RSI6回落70下方",
                "changed": True}
    if not top_divergence:
        return {"state": STATE_RELEASED, "from_state": STATE_SUSPECT,
                "reason": RELEASE_REASONS[1], "rule": "解除-顶背驰消失",
                "changed": True}
    vr = _safe_float(volume_ratio)
    pct = _safe_float(price_change_pct)
    c, m = _safe_float(close), _safe_float(ma5)
    if vr is not None and vr < 1.0 and pct is not None and pct < 0 \
            and c is not None and m is not None and c >= m:
        return {"state": STATE_RELEASED, "from_state": STATE_SUSPECT,
                "reason": RELEASE_REASONS[2], "rule": "解除-缩量回踩不破MA5",
                "changed": True}
    return {"state": STATE_SUSPECT, "from_state": STATE_SUSPECT,
            "reason": "无新证据", "rule": "延续嫌疑", "changed": False}


def record_distribution_state(
    stock_code: str,
    stock_name: str,
    *,
    as_of: str,
    rsi6=None,
    top_divergence: bool = False,
    price_change_pct: Optional[float] = None,
    close=None,
    ma5=None,
    volume_ratio: Optional[float] = None,
    main_force_net: Optional[float] = None,
    main_force_prev: Optional[float] = None,
    block_trade_trigger: bool = False,
    institutional_reduce: bool = False,
) -> Dict[str, Any]:
    """读取当前状态→评估→写快照；状态变化时写转换日志。"""
    code = str(stock_code).zfill(6)
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            "SELECT state FROM distribution_watch WHERE stock_code=?",
            (code,),
        )
        row = cursor.fetchone()
        current = row[0] if row else STATE_NONE
        result = evaluate_distribution_state(
            current,
            rsi6=rsi6, top_divergence=top_divergence,
            price_change_pct=price_change_pct, close=close, ma5=ma5,
            volume_ratio=volume_ratio, main_force_net=main_force_net,
            main_force_prev=main_force_prev,
            block_trade_trigger=block_trade_trigger,
            institutional_reduce=institutional_reduce,
        )
        state = result["state"]
        cursor.execute(
            """
            INSERT OR REPLACE INTO distribution_watch
            (stock_code, stock_name, state, reason, rule_entry, as_of, entered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code, stock_name or "", state, result.get("reason", ""),
                result.get("rule", ""), as_of,
                as_of if current == STATE_NONE else as_of,
            ),
        )
        cursor.execute(
            """
            INSERT OR REPLACE INTO distribution_watch_snapshot
            (as_of, stock_code, stock_name, state, reason, rule_entry)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (as_of, code, stock_name or "", state,
             result.get("reason", ""), result.get("rule", "")),
        )
        if result.get("changed") and current != state:
            cursor.execute(
                """
                INSERT INTO distribution_watch_logs
                (stock_code, stock_name, from_state, to_state, as_of,
                 reason, rule_entry, trigger_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code, stock_name or "", current, state, as_of,
                    result.get("reason", ""), result.get("rule", ""),
                    "{}",
                ),
            )
        conn.commit()
    return result


def get_distribution_state(stock_code: str) -> Optional[Dict[str, Any]]:
    code = str(stock_code).zfill(6)
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            """
            SELECT stock_code, stock_name, state, reason, rule_entry, as_of
            FROM distribution_watch WHERE stock_code=?
            """,
            (code,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {
        "stock_code": row[0], "stock_name": row[1], "state": row[2],
        "reason": row[3], "rule_entry": row[4], "as_of": row[5],
    }


def list_active_suspects(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """当前处于嫌疑态的票；as_of 给定时按快照查当日嫌疑。"""
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        if as_of:
            cursor.execute(
                """
                SELECT stock_code, stock_name, state, reason, rule_entry
                FROM distribution_watch_snapshot
                WHERE as_of=? AND state=?
                """,
                (as_of, STATE_SUSPECT),
            )
        else:
            cursor.execute(
                """
                SELECT stock_code, stock_name, state, reason, rule_entry
                FROM distribution_watch WHERE state=?
                """,
                (STATE_SUSPECT,),
            )
        rows = cursor.fetchall()
    return [
        {
            "stock_code": r[0], "stock_name": r[1], "state": r[2],
            "reason": r[3], "rule_entry": r[4],
        }
        for r in rows
    ]


def list_transitions(as_of: str, lookback_days: int = 1) -> List[Dict[str, Any]]:
    start = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute(
            """
            SELECT stock_code, stock_name, from_state, to_state, as_of,
                   reason, rule_entry
            FROM distribution_watch_logs
            WHERE as_of >= ? ORDER BY as_of DESC, id DESC
            """,
            (start,),
        )
        rows = cursor.fetchall()
    return [
        {
            "stock_code": r[0], "stock_name": r[1], "from_state": r[2],
            "to_state": r[3], "as_of": r[4], "reason": r[5],
            "rule_entry": r[6],
        }
        for r in rows
    ]


def render_distribution_summary(as_of: str) -> str:
    """渲染派发状态机摘要：活跃嫌疑、转换日志、昨日嫌疑今日终态/延续。"""
    lines = ["🕵️ 派发状态机:"]
    active = list_active_suspects()
    if active:
        parts = []
        for s in active:
            name = s.get("stock_name") or s.get("stock_code")
            parts.append(f"{name}({s.get('stock_code')})")
        lines.append(f"  活跃嫌疑({len(active)}): " + " | ".join(parts))
    else:
        lines.append("  活跃嫌疑: 无")

    transitions = list_transitions(as_of, lookback_days=1)
    if transitions:
        for t in transitions:
            name = t.get("stock_name") or t.get("stock_code")
            lines.append(
                f"  {name}({t.get('stock_code')}) "
                f"{_STATE_LABELS.get(t['from_state'], t['from_state'])}→"
                f"{_STATE_LABELS.get(t['to_state'], t['to_state'])} "
                f"[{t.get('as_of')}] {t.get('reason')}"
            )
    else:
        lines.append("  今日转换: 无")

    yesterday = (date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
    y_suspects = list_active_suspects(as_of=yesterday)
    if y_suspects:
        today_state = {
            s["stock_code"]: s for s in list_active_suspects(as_of=as_of)
        }
        y_rows = []
        for s in y_suspects:
            code = s.get("stock_code")
            if code in today_state:
                y_rows.append(f"{s.get('stock_name') or code}→延续嫌疑")
            else:
                st = get_distribution_state(code)
                state = (st or {}).get("state", "无记录")
                y_rows.append(
                    f"{s.get('stock_name') or code}→"
                    f"{_STATE_LABELS.get(state, state)}"
                )
        lines.append(f"  昨日嫌疑今日: " + " | ".join(y_rows))
    return "\n".join(lines)


__all__ = [
    "STATE_NONE", "STATE_SUSPECT", "STATE_CONFIRMED", "STATE_RELEASED",
    "evaluate_distribution_state", "record_distribution_state",
    "get_distribution_state", "list_active_suspects", "list_transitions",
    "render_distribution_summary",
]
