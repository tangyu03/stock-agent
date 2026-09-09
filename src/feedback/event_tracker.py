"""
昨日事件追踪表（决策记录 P1-3）— 让系统的交易可审计
=====================================================

依据：
  9/3 的 6 条信号在 9/4 全灭，系统没有任何回头看的行为——
  没有记忆的系统无法校准任何参数，包括其他所有条目的作废条件判定
  都依赖这张表先存在。

反方（已对冲）：
  唯一的成本是版面和每天几分钟的维护；风险是沦为"记录了但不分析"
  的装饰——验收标准里包含"每月一次按表校准参数"的动作，否则表格
  本身作废。

作废条件：
  无（本条是其他条目的基础设施）。唯一会修改它的场景是系统迁移到
  专门的回测平台，届时表格升级为数据库。

数据源：
  - signal_events（事件生命周期：诞生/triggered/invalidated/expired）
  - trade_logs（回执闭环：了结记录 pnl_pct、Z/W 触发、归因）
  验证：下一份收盘版顶部出现表格；9/3→9/4 的六条信号有闭合记录
  （状态：了结，浮亏 -3~-8%）。
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from ..analyzers.signal_lifecycle import display_status, normalize_status

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "valid": "已触发",
    "filled": "已成交",
    "invalidated": "失效撤单",
    "expired": "过期作废",
}

_TRACKING_STATUS_LABELS = {
    "valid": "待回踩",
    "filled": "已成交",
    "invalidated": "失效撤单",
    "expired": "过期作废",
}

_EVENT_STATUS_LABELS = {
    "valid": "已触发",
    "filled": "已成交",
    "invalidated": "已撤单",
    "expired": "已过期",
}


@dataclass
class TrackedEvent:
    """一行追踪记录（事件链路视角）"""
    stock_code: str = ""
    stock_name: str = ""
    entry_type: str = ""
    born_date: str = ""
    status: str = ""                # 待回踩/已入场/失效撤单/过期作废/了结
    outcome: str = ""               # 浮盈/浮亏/盈亏%（了结时）
    link: str = ""                  # 链路（止损→再评估 之类的事件对）
    detail: str = ""


def _to_date(text: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)[:10])
    except (ValueError, TypeError):
        return None


def build_event_tracking_table(
    as_of: Optional[date] = None,
    lookback_days: int = 2,
    include_open: bool = True,
    events: Optional[List] = None,
    closed_trades: Optional[List[Dict]] = None,
) -> List[Dict]:
    """构建昨日（及近 N 日）事件追踪表。

    行结构：{date, stock_code, stock_name, entry_type, status, outcome, link, detail}
    优先级：闭合交易（了结）> 事件状态迁移（撤单/过期/待回踩）。
    events 可注入（测试/回放）；缺省读 signal_events 表。
    """
    as_of = as_of or date.today()
    window_start = as_of - timedelta(days=lookback_days)
    rows: List[Dict] = []

    # ── 1. 闭合交易（回执闭环：9/3→9/4 六条信号全灭的闭合记录） ──
    trades = closed_trades
    if trades is None:
        try:
            from ..feedback.trade_logger import get_trade_logger
            trades = get_trade_logger().get_closed_trades()
        except Exception as e:
            logger.debug("读取闭合交易失败: %s", str(e)[:60])
            trades = []
    for t in trades or []:
        exit_day = _to_date(t.get("exit_date") or "")
        born_day = _to_date(t.get("trigger_date") or t.get("entry_date") or "")
        if exit_day is None and born_day is None:
            continue
        ref_day = exit_day or born_day
        if ref_day and not (window_start <= ref_day <= as_of):
            continue
        pnl = t.get("pnl_pct")
        outcome = (
            f"浮盈{float(pnl):+.1f}%" if pnl is not None and float(pnl) > 0
            else f"浮亏{float(pnl):+.1f}%" if pnl is not None
            else "未回填"
        )
        zw = []
        if t.get("zw_triggered"):
            zw.append("Z触发(认错)")
        if t.get("exit_type"):
            zw.append(str(t.get("exit_type")))
        rows.append({
            "date": (exit_day or born_day).isoformat(),
            "stock_code": t.get("stock_code", ""),
            "stock_name": t.get("stock_name", ""),
            "entry_type": t.get("strategy") or t.get("entry_type") or "",
            "status": "了结",
            "outcome": outcome,
            "link": (
                f"{(born_day or exit_day).isoformat()}入场→{exit_day.isoformat() if exit_day else '?'}离场"
                + (f"，{'，'.join(zw)}" if zw else "")
            ),
            "detail": str(t.get("review_note") or t.get("note") or ""),
        })

    # ── 2. 信号事件生命周期（含未入场的撤单/过期——审计链不只覆盖成交） ──
    if events is None:
        try:
            from ..db import get_conn
            from .signal_lifecycle import _ensure_tables
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM signal_events WHERE born_date >= ? ORDER BY born_date, stock_code",
                    (window_start.isoformat(),),
                )
                events = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.debug("读取信号事件失败（追踪表降级）: %s", str(e)[:60])
            events = []

    # 事件列表元素可能是 dict（DB 行）或 SignalEvent 对象，统一取字段
    normalized: List[Dict] = []
    for e in events or []:
        if isinstance(e, dict):
            normalized.append(e)
        else:
            normalized.append({
                "born_date": getattr(e, "born_date", ""),
                "stock_code": getattr(e, "stock_code", ""),
                "stock_name": getattr(e, "stock_name", ""),
                "entry_type": getattr(e, "entry_type", ""),
                "status": getattr(e, "status", ""),
                "invalid_reason": getattr(e, "invalid_reason", ""),
                "event_id": getattr(e, "event_id", ""),
            })

    covered_keys = {(r["stock_code"], r.get("entry_type") or r["entry_type"]) for r in rows}
    for e in normalized:
        born_day = _to_date(e.get("born_date"))
        if born_day is None or not (window_start <= born_day <= as_of):
            continue
        code = e.get("stock_code", "")
        etype = e.get("entry_type", "")
        if (code, etype) in covered_keys:
            continue  # 闭合交易已覆盖同一事件
        raw_status = str(e.get("status") or "valid")
        normalized_status = normalize_status(raw_status)
        status = _TRACKING_STATUS_LABELS.get(
            normalized_status,
            display_status(normalized_status),
        )
        if status in ("待回踩", "已成交") and not include_open:
            continue
        rows.append({
            "date": born_day.isoformat(),
            "stock_code": code,
            "stock_name": e.get("stock_name") or "",
            "entry_type": etype,
            "status": status,
            "outcome": str(e.get("invalid_reason") or ""),
            "link": f"事件{e.get('event_id', '')[-8:]} 诞生{born_day.isoformat()}",
            "detail": "",
        })

    rows.sort(key=lambda r: (r.get("date", ""), r.get("stock_code", "")))
    return rows


def render_event_tracking_table(rows: List[Dict]) -> str:
    """渲染为推送文本（收盘版顶部）。

    用法与任务 4.1 的镜像关系：事件追踪表让系统的交易可审计，
    决策记录让改造建议本身可审计。
    """
    if not rows:
        return (
            "  近2日无信号事件与了结记录\n"
            "  （记录闭环从第一笔信号开始积累；每月一次按表校准参数，"
            "否则表格本身作废）"
        )
    lines = []
    for r in rows:
        outcome = r.get("outcome") or ""
        link = r.get("link") or ""
        seg = (
            f"  {r.get('date', '')} {r.get('stock_name', '')}({r.get('stock_code', '')}) "
            f"[{r.get('entry_type', '')}] {r.get('status', '')}"
        )
        if outcome:
            seg += f" | {outcome}"
        if link:
            seg += f" | {link}"
        lines.append(seg)
    lines.append(
        "  验收：每月一次按表校准参数（拒绝无数据的调参）；"
        "止损→再评估的链路在表内闭合"
    )
    return "\n".join(lines)


def collect_in_flight_events(
    as_of: Optional[date] = None,
    lookback_days: int = 7,
    events: Optional[List] = None,
) -> List[Dict]:
    """广播表数据源：只读事件，不读持仓、不推断仓位。

    含 valid/filled；近期 invalidated/expired 也显示，用来解释为什么
    事件从在飞表里消失。events 可注入（测试/回放）。
    """
    as_of = as_of or date.today()
    if events is None:
        try:
            from ..db import get_conn
            from ..analyzers.signal_lifecycle import _ensure_tables
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM signal_events WHERE born_date >= ? "
                    "ORDER BY born_date DESC, stock_code",
                    ((as_of - timedelta(days=lookback_days)).isoformat(),),
                )
                events = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.debug("读取在飞事件失败（广播表降级）: %s", str(e)[:60])
            events = []

    rows: List[Dict] = []
    for e in events or []:
        if isinstance(e, dict):
            row = dict(e)
        else:
            row = {
                "event_id": getattr(e, "event_id", ""),
                "stock_code": getattr(e, "stock_code", ""),
                "stock_name": getattr(e, "stock_name", ""),
                "entry_type": getattr(e, "entry_type", ""),
                "born_date": getattr(e, "born_date", ""),
                "expire_date": getattr(e, "expire_date", ""),
                "entry_price": getattr(e, "entry_price", 0),
                "stop_loss": getattr(e, "stop_loss", 0),
                "target_low": getattr(e, "target_low", 0),
                "target_high": getattr(e, "target_high", 0),
                "status": getattr(e, "status", ""),
                "invalid_reason": getattr(e, "invalid_reason", ""),
            }
        born = _to_date(row.get("born_date"))
        if born is None:
            continue
        status = normalize_status(str(row.get("status") or "valid"))
        if born > as_of or (
            status in ("invalidated", "expired")
            and born < as_of - timedelta(days=lookback_days)
        ):
            continue
        row["born_date"] = born.isoformat()
        row["status_label"] = _EVENT_STATUS_LABELS.get(status, display_status(status))
        row["active"] = status in ("valid", "filled")
        rows.append(row)

    priority = {"valid": 0, "filled": 1, "invalidated": 2, "expired": 3}
    rows.sort(key=lambda r: (priority.get(r["status"], 9), r["born_date"], r["stock_code"]))
    return rows


def render_in_flight_events(rows: List[Dict]) -> str:
    """渲染广播表（HTML），顶部第一段就是事件状态，不再是持仓散文。"""
    if not rows:
        return "<b>在飞事件 0</b><br/>&nbsp;&nbsp;今日无合格事件；空仓是合法输出<br/>"
    active = sum(1 for r in rows if r.get("active"))
    parts = [f"<b>在飞事件 {len(rows)}（活跃{active}）</b>"]
    for r in rows:
        try:
            entry = float(r.get("entry_price") or 0)
            stop = float(r.get("stop_loss") or 0)
            target = float(r.get("target_low") or 0)
        except (TypeError, ValueError):
            continue
        current = None
        try:
            raw_current = r.get("current_price")
            current = float(raw_current) if raw_current not in (None, "") else None
        except (TypeError, ValueError):
            current = None
        seg = (
            f"&nbsp;&nbsp;{r.get('stock_name') or r.get('stock_code')} "
            f"{r.get('entry_type') or '-'} {r.get('status_label')}"
        )
        if r.get("event_id"):
            seg += f" #{str(r.get('event_id'))[-8:]}"
        if entry:
            seg += f" 买{entry:.2f}"
        if stop:
            seg += f" 止损{stop:.2f}"
        if target:
            seg += f" 目标{target:.2f}"
        if current:
            seg += f" 现价{current:.2f}"
            if entry:
                distance = (current / entry - 1.0) * 100
                seg += f" 距买点{distance:+.1f}%"
        if r.get("expire_date"):
            seg += f" 至{r.get('expire_date')}"
        if r.get("status") in ("invalidated", "expired") and r.get("invalid_reason"):
            reason = str(r["invalid_reason"])
            seg += f" ({reason[:42]})"
        parts.append(seg)
    return "<br/>".join(parts) + "<br/>"


def _normalize_missing_condition(value: str) -> str:
    """把拦截句压缩成可盯的条件名，拒绝“未触发”这类无行动信息。"""
    text = str(value or "").strip()
    replacements = {
        "正常行情，未触发": "恐慌环境",
        "ADX单边力度": "ADX",
        "量能(外推或实际)": "量能",
        "周线MACD未向上": "周线MACD向上",
        "未出现低吸形态": "低吸形态",
        "非MA多头排列": "MA多头排列",
        "未站上MA25": "站上MA25",
    }
    for raw, label in replacements.items():
        text = text.replace(raw, label)
    text = re.sub(r"^量未破", "量破", text)
    text = re.sub(r"RSI14未过热(\([^)]*\))?", "RSI未过热", text)
    return text.strip()


def _strategy_missing_groups(diagnostic: str) -> List[Dict]:
    """把策略拦截明细拆成 {strategy, conditions}，供“最接近策略”排序。"""
    text = str(diagnostic or "")
    if not text:
        return []
    if "策略检查" not in text:
        first = text.splitlines()[0].strip()
        condition = _normalize_missing_condition(first)
        return [{"strategy": "综合", "conditions": [condition]}] if condition else []

    strategy_part = text.split("策略检查", 1)[1].split("评分:", 1)[0]
    groups: List[Dict] = []
    for line in strategy_part.splitlines():
        if not line.strip().startswith("- "):
            continue
        item = line.strip()[2:].strip()
        if ":" in item:
            strategy, detail = item.split(":", 1)
        else:
            strategy, detail = "综合", item
            if groups and groups[-1]["strategy"] == "综合":
                groups[-1]["conditions"].extend(
                    _normalize_missing_condition(raw)
                    for raw in re.split(r"[;；、]", detail)
                    if _normalize_missing_condition(raw)
                )
                continue
        detail = detail.split("——未过:", 1)[-1]
        detail = re.sub(r"门[一二三]未过:\s*", "", detail)
        conditions = []
        for raw in re.split(r"[;；、]", detail):
            condition = _normalize_missing_condition(raw)
            if condition:
                conditions.append(condition)
        if conditions:
            groups.append({"strategy": strategy.strip(), "conditions": conditions})
    return groups


def _failed_conditions(diagnostic: str) -> List[str]:
    """兼容旧调用：返回最接近策略的缺失条件。"""
    groups = _strategy_missing_groups(diagnostic)
    if not groups:
        return []
    closest = min(groups, key=lambda group: len(group["conditions"]))
    return closest["conditions"]


def build_watch_ladder(
    entry_diagnostics: Dict[str, str],
    stocks: List[Dict],
    in_flight_events: Optional[List[Dict]] = None,
) -> List[Dict]:
    """27×5 矩阵压成一张候梯表：谁差几个条件，谁排前面。"""
    rows: List[Dict] = []
    in_flight_by_code: Dict[str, Dict] = {}
    for event in in_flight_events or []:
        code = str(event.get("stock_code", ""))
        if code and event.get("active") and code not in in_flight_by_code:
            in_flight_by_code[code] = event
    for stock in stocks or []:
        code = str(stock.get("code", ""))
        if not code:
            continue
        groups = _strategy_missing_groups((entry_diagnostics or {}).get(code, ""))
        if not groups:
            continue
        closest = min(groups, key=lambda group: len(group["conditions"]))
        failures = closest["conditions"]
        shown = "+".join(failures[:2])
        if len(failures) > 2:
            shown += f"等{len(failures)}项"
        in_flight = in_flight_by_code.get(code, {})
        reason = f"{closest['strategy']}：缺 {shown}"
        if in_flight:
            reason += (
                f" ⚠在飞：{in_flight.get('entry_type') or '未知策略'}"
                f"·{in_flight.get('status_label') or '活跃'}"
            )
            if in_flight.get("event_id"):
                reason += f"#{str(in_flight['event_id'])[-8:]}"
        rows.append({
            "stock_code": code,
            "code": code,
            "stock_name": stock.get("name", code),
            "name": stock.get("name", code),
            "fail_count": len(failures),
            "strategy": closest["strategy"],
            "failures": failures,
            "reason": reason,
            "in_flight": bool(in_flight),
            "in_flight_event_id": str(in_flight.get("event_id", "")),
            "in_flight_entry_type": str(in_flight.get("entry_type", "")),
            "in_flight_status_label": str(in_flight.get("status_label", "")),
        })
    return sorted(rows, key=lambda r: (r["fail_count"], r["stock_code"]))


def build_virtual_fill_counts(rows: List[Dict], target: int = 30) -> str:
    """撮合计数表：先只数数，不做 EV/滑点/分桶。"""
    if not rows:
        return "撮合计数: 样本0/30"
    counts = {"filled": 0, "stop": 0, "cancelled": 0, "expired": 0, "open": 0}
    for r in rows:
        status = normalize_status(str(r.get("status") or ""))
        reason = str(r.get("invalid_reason") or "")
        if status == "filled":
            counts["filled"] += 1
        elif status == "valid":
            counts["open"] += 1
        elif status == "invalidated" and "止损线" in reason:
            counts["stop"] += 1
        elif status == "invalidated" and "回踩失败" in reason:
            counts["cancelled"] += 1
        elif status == "expired":
            counts["expired"] += 1
    total = sum(counts.values())
    return (
        f"撮合计数: 成交{counts['filled']} 撤单{counts['cancelled']} "
        f"止损{counts['stop']} 过期{counts['expired']} 在飞{counts['open']} "
        f"| 样本{total}/{target}"
    )
