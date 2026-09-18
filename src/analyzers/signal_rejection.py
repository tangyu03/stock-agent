"""
信号出厂拒绝留痕模块（signal_rejections 表）

将“买入信号被拒”独立成模块，便于审计与复盘：
- 运行期暂存：RejectionLedger（timing_engine 产出 → unified_engine 采集）
- 落库：persist_rejection（engine 统一写入 signal_rejections 表）
- 查询：query_rejections（按股票/日期审计）

数据流：
  timing_engine.check_entry_signals
    → ledger.record(stock_code, rejection)
    → unified_engine: ledger.pop(code) → batch.rejected
    → engine: persist_rejection(rejection) → signal_rejections 表
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import get_conn

logger = logging.getLogger(__name__)

# 假说四要素 → 用于 missing_fields 标注
_HYPOTHESIS_KEYS = (("X", "x"), ("Y", "y"), ("Z", "z"), ("W", "w"))


class RejectionLedger:
    """运行期被拒信号暂存器（每轮分析前清空）。"""

    def __init__(self) -> None:
        self._pending: Dict[str, Dict[str, Any]] = {}

    @property
    def pending(self) -> Dict[str, Dict[str, Any]]:
        return self._pending

    def record(self, stock_code: str, rejection: Dict[str, Any]) -> None:
        self._pending[str(stock_code)] = rejection

    def pop(self, stock_code: str) -> Optional[Dict[str, Any]]:
        return self._pending.pop(str(stock_code), None)

    def drain(self) -> List[Dict[str, Any]]:
        items = list(self._pending.values())
        self._pending.clear()
        return items

    def clear(self) -> None:
        self._pending.clear()


def persist_rejection(rejection: Dict[str, Any]) -> Optional[int]:
    """将被拒信号写入 signal_rejections 表；返回新记录 id，失败返回 None。"""
    try:
        hypothesis = rejection.get("hypothesis") or {}
        missing = [
            label for label, key in _HYPOTHESIS_KEYS
            if not hypothesis.get(key)
        ]
        detail = {
            "benchmark_price": rejection.get("benchmark_price", 0),
            "stop_loss": rejection.get("stop_loss", 0),
            "target_range": rejection.get("target_range", []),
            "hypothesis": hypothesis,
            "fundamental": rejection.get("fundamental"),
            "fundamental_rejected": rejection.get("fundamental_rejected", False),
            "valuation": rejection.get("valuation"),
            "valuation_rejected": rejection.get("valuation_rejected", False),
        }
        reasons = rejection.get("reasons") or []
        if not reasons and rejection.get("reason"):
            reasons = [rejection["reason"]]
        with get_conn() as conn:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute(
                """INSERT INTO signal_rejections
                (date, time, stock_code, stock_name, entry_type,
                 missing_fields, reason, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M:%S"),
                    rejection.get("stock_code", ""),
                    rejection.get("stock_name", ""),
                    rejection.get("entry_type", ""),
                    ",".join(missing),
                    "; ".join(str(r) for r in reasons),
                    json.dumps(detail, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error("记录拒绝留痕失败: %s", e)
        return None


def query_rejections(
    stock_code: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """审计查询 signal_rejections 表（按股票/日期过滤）。"""
    clauses: List[str] = []
    params: List[Any] = []
    if stock_code:
        clauses.append("stock_code = ?")
        params.append(stock_code)
    if start_date:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date <= ?")
        params.append(end_date)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM signal_rejections{where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            )
            columns = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                item = dict(zip(columns, row))
                try:
                    item["detail"] = json.loads(item.get("detail") or "{}")
                except (TypeError, ValueError):
                    pass
                rows.append(item)
            return rows
    except Exception as e:
        logger.error("查询拒绝留痕失败: %s", e)
        return []
