"""
信号生命周期管理 — 把"状态"改造成"事件"
================================================

"站上 MA25"是状态，今天为真、明天也为真，于是沃尔德连发两天买入信号，
持仓者被当成空仓者反复推销。厘清的办法是给信号加上生命周期：

  - 诞生   突破发生当日（事件边界由策略检查保证），且当日收阳、量能确认
  - 有效期 N 日内回踩买点有效，超期作废
  - 失效   收盘跌回突破位、或板块状态机转为退潮，立即撤单
  - 受众   只对空仓者成立；对持仓者的输出永远是持有/加仓/减仓/止损四选一
           （受众路由在 live_scheduler / engine 层实现）

事件化之后，"信号不会死"的问题自动消失：昨天的信号今天只剩演化路径，
不会再原样重播。
"""
import json
import re
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from ..db import get_conn
from ..rules_version import get_rules_version

logger = logging.getLogger(__name__)

# 状态语义的唯一解释：valid 是“已触发、买单等待回踩”；
# filled 是“价格已触及买点”；triggered 只是旧库里的 filled 别名。
TERMINAL_REENTRY_STATUSES = ("invalidated",)
ACTIVE_EVENT_STATUSES = ("valid", "filled", "frozen", "pending_confirm")
LEGACY_TRIGGERED_STATUS = "triggered"
TERMINAL_EVENT_STATUSES = (
    "invalidated", "expired", "chase_abandon",
    "sig_target", "sig_stop", "time_exit",
)

# 推送层不暴露内部枚举；状态机仍是英文键，展示前统一翻译。
_STATUS_DISPLAY = {
    "valid": "已触发",
    "filled": "已成交",
    "frozen": "已冻结",
    "pending_confirm": "待确认",
    "invalidated": "已失效",
    "expired": "已过期",
    "chase_abandon": "追高放弃",
    "sig_target": "信号止盈",
    "sig_stop": "信号止损",
    "time_exit": "时间离场",
}

# P2-15：完结原因两级枚举（一级=盈亏归属/作废/时间，二级=具体状态）。
_COMPLETION_LEVEL1 = {
    "sig_target": "止盈",
    "sig_stop": "止损",
    "chase_abandon": "作废",
    "expired": "作废",
    "invalidated": "作废",
    "time_exit": "时间",
}


def normalize_status(status: str) -> str:
    """把旧库 triggered 归一为 filled，避免展示层各说各话。"""
    value = str(status or "")
    return "filled" if value == LEGACY_TRIGGERED_STATUS else value


def can_transition_status(current: str, target: str) -> bool:
    """事件状态只允许前进；终态不允许被任何模块改写。"""
    current = normalize_status(current)
    target = normalize_status(target)
    if current == target:
        return True
    if current == "valid":
        return target in ("filled", "frozen", "pending_confirm", *TERMINAL_EVENT_STATUSES)
    if current == "filled":
        return target in (
            "frozen", "pending_confirm", "sig_target", "sig_stop", "time_exit",
        )
    if current == "frozen":
        return target in ("valid", "filled", *TERMINAL_EVENT_STATUSES)
    if current == "pending_confirm":
        return target in ("valid", "filled", *TERMINAL_EVENT_STATUSES)
    return False
_RULE_VERSION_DISPLAY = {
    "buffered_structure": "结构位加缓冲",
    "bare_structure": "结构位本体",
    "atr_buffer": "ATR缓冲",
}


def display_status(status: str) -> str:
    normalized = normalize_status(status)
    return _STATUS_DISPLAY.get(normalized, normalized)


def completion_reason(status: str) -> str:
    """完结原因两级枚举：一级(二级)，如 止盈(信号止盈)、作废(已过期)、时间(时间离场)。
    非完结状态退化为一层（沿用 display_status），保证调用点不崩。
    """
    normalized = normalize_status(status)
    level1 = _COMPLETION_LEVEL1.get(normalized)
    if not level1:
        return display_status(normalized)
    return f"{level1}({display_status(normalized)})"


def display_rule_version(rule_version: str) -> str:
    return _RULE_VERSION_DISPLAY.get(str(rule_version or ""), str(rule_version or ""))


def _snapshot_flags(snapshot: dict, maintenance: bool = False) -> List[str]:
    flags: List[str] = []
    try:
        # C-修复：维持面板量比必须有口径标注（沃尔德9-18 量比4.69 vs 同期量比1.57/
        # 接口量比2.33 并存无口径）；早盘优先展示同期量比，其余用有效口径并落库口径名。
        if maintenance:
            early = bool(snapshot.get("is_early_window"))
            sp_ratio = snapshot.get("same_period_volume_ratio")
            eff_ratio = snapshot.get("volume_ratio_effective")
            if early and sp_ratio is not None:
                volume_ratio = sp_ratio
                caliber = snapshot.get("same_period_caliber") or "同期量比"
            else:
                volume_ratio = eff_ratio if eff_ratio is not None else snapshot.get("volume_ratio")
                caliber = snapshot.get("volume_ratio_caliber") or ""
        else:
            volume_ratio = snapshot.get("volume_ratio")
            caliber = ""
        if volume_ratio is not None:
            text = f"量比{float(volume_ratio):.2f}"
            # 仅当快照确实带口径时才标注，旧快照（无口径字段）保持原格式
            if maintenance and caliber:
                text += f"({caliber})"
            if maintenance and float(volume_ratio) < 1.2:
                text += " △缩量"
            else:
                text += "✓"
            flags.append(text)
    except (TypeError, ValueError):
        pass
    ma = snapshot.get("ma_alignment")
    if ma is not None:
        flags.append(("多头排列✓" if ma else "多头排列✗"))
    rsi = snapshot.get("rsi")
    if rsi is not None:
        flags.append(f"RSI{float(rsi):.1f}")
    sector = snapshot.get("sector_status")
    if sector:
        flags.append(f"板块{sector}")
    return flags


def render_entry_and_maintenance(event) -> str:
    """T0入场条件与当日维持条件分栏，禁止用当日值改写T0事实。"""
    try:
        entry = json.loads(event.entry_snapshot or "{}")
    except Exception:
        entry = {}
    try:
        maintenance = json.loads(event.maintenance_check or "{}")
    except Exception:
        maintenance = {}
    entry_flags = _snapshot_flags(entry)
    maintenance_flags = _snapshot_flags(maintenance, maintenance=True)
    if maintenance_flags and entry_flags:
        weak = any("△" in flag for flag in maintenance_flags)
        suffix = (
            "——维持条件走弱，不影响已触发状态，仅提示"
            if weak else "——维持条件正常"
        )
    else:
        suffix = "——维持检查未评估"
    return (
        f"入场(T0 {event.born_date}): {' '.join(entry_flags) or '快照缺失'} | "
        f"维持(今日): {' '.join(maintenance_flags) or '未评估'}{suffix}"
    )


def localize_display_enums(value) -> str:
    """把自由展示文案中残留的生命周期/规则枚举翻译成中文。"""
    text = str(value or "")
    text = re.sub(r"\btriggered\b", _STATUS_DISPLAY["filled"], text)
    for enum, label in _STATUS_DISPLAY.items():
        text = re.sub(rf"\b{re.escape(enum)}\b", label, text)
    for enum, label in _RULE_VERSION_DISPLAY.items():
        text = re.sub(rf"\b{re.escape(enum)}\b", label, text)
    return text


@dataclass
class SignalEvent:
    """一个信号事件（有生命周期，非状态）"""
    event_id: str
    stock_code: str
    stock_name: str = ""
    entry_type: str = ""
    born_date: str = ""
    expire_date: str = ""
    breakout_level: float = 0.0     # 失效判定锚：收盘跌回此位 → 撤单
    entry_price: float = 0.0        # Y: 买点（N 日内回踩有效）
    stop_loss: float = 0.0          # Z: 配对认错价
    target_low: float = 0.0         # W: 兑现区间下沿
    target_high: float = 0.0        # W: 兑现区间上沿
    hypothesis_x: str = ""
    hypothesis_y: str = ""
    hypothesis_z: str = ""
    hypothesis_w: str = ""
    status: str = "valid"           # valid / filled / invalidated / expired（triggered 为旧别名）
    invalid_reason: str = ""
    # 【Phase5 回炉】事件生成时的 Z 线规则版本（z_line_mode）。
    # 存量事件与新规则双轨审计：旧事件（空串）渲染为“旧版规则”，
    # 避免审计者误判“Z 线统一没做”（博杰 9/7 旧 Z=85.54 实证）。
    rule_version: str = ""
    rules_version: str = ""
    frozen_prev_status: str = ""
    entry_snapshot: str = "{}"
    maintenance_check: str = "{}"
    y_formula: str = ""
    y_inputs: str = ""


def _ensure_tables(cursor) -> None:
    """幂等建表（首次使用可能早于 init_db，如仅运行信号扫描）"""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_events (
        event_id TEXT PRIMARY KEY,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        entry_type TEXT NOT NULL,
        born_date TEXT NOT NULL,
        expire_date TEXT,
        breakout_level REAL,
        entry_price REAL,
        stop_loss REAL,
        target_low REAL,
        target_high REAL,
        hypothesis_x TEXT,
        hypothesis_y TEXT,
        hypothesis_z TEXT,
        hypothesis_w TEXT,
        status TEXT DEFAULT 'valid',
        invalid_reason TEXT,
        rule_version TEXT DEFAULT '',
        rules_version TEXT DEFAULT '',
        frozen_prev_status TEXT DEFAULT '',
        entry_snapshot TEXT DEFAULT '{}',
        maintenance_check TEXT DEFAULT '{}',
        y_formula TEXT DEFAULT '',
        y_inputs TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # 【Phase5 回炉】旧库幂等补列（存量事件 rule_version='' → 渲染为“旧版规则”）
    try:
        cursor.execute("ALTER TABLE signal_events ADD COLUMN rule_version TEXT DEFAULT ''")
    except Exception:
        pass  # 列已存在（SQLite 无 ADD COLUMN IF NOT EXISTS）

    try:
        cursor.execute("ALTER TABLE signal_events ADD COLUMN rules_version TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        cursor.execute("ALTER TABLE signal_events ADD COLUMN frozen_prev_status TEXT DEFAULT ''")
    except Exception:
        pass
    for column in ("entry_snapshot", "maintenance_check"):
        try:
            cursor.execute(f"ALTER TABLE signal_events ADD COLUMN {column} TEXT DEFAULT '{{}}'")
        except Exception:
            pass

    # 买点公式审计：事件出生时的 Y 定位必须可回放。
    for column in ("y_formula", "y_inputs"):
        try:
            cursor.execute(f"ALTER TABLE signal_events ADD COLUMN {column} TEXT DEFAULT ''")
        except Exception:
            pass

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_code_status "
                   "ON signal_events(stock_code, status)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_event_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        reason TEXT DEFAULT '',
        source TEXT DEFAULT 'signal_lifecycle',
        trigger_data TEXT DEFAULT '{}',
        rule_entry TEXT DEFAULT '',
        rules_version TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_event_logs_event "
                   "ON signal_event_logs(event_id, created_at)")
    for column in ("trigger_data", "rule_entry", "rules_version"):
        try:
            default = "'{}'" if column == "trigger_data" else "''"
            cursor.execute(
                f"ALTER TABLE signal_event_logs ADD COLUMN {column} TEXT DEFAULT {default}"
            )
        except Exception:
            pass

    from ..feedback.signal_ledger import ensure_signal_ledger_schema
    ensure_signal_ledger_schema(cursor)


def _default_rule_entry(current: str, target: str, reason: str = "") -> str:
    """Known lifecycle edges; free-text migration callers get a stable fallback."""
    current = normalize_status(current)
    target = normalize_status(target)
    if current == "none" and target == "valid":
        return "R1.0事件诞生"
    if target == "filled":
        return "R3.2回踩确认"
    if target == "expired":
        return "R3.5有效期到期"
    if target == "chase_abandon":
        return "R3.6追高放弃"
    if target == "sig_target":
        return "R3.7信号止盈"
    if target == "sig_stop":
        return "R3.8信号止损"
    if target == "time_exit":
        return "R3.9时间离场"
    if target == "frozen":
        return "R4.1事件冻结"
    if target == "pending_confirm":
        return "R3.10外推量能待确认"
    if current == "pending_confirm" and target == "invalidated":
        return "R3.11外推量能次日不足"
    if current == "pending_confirm" and target in ("valid", "filled"):
        return "R3.12外推量能确认恢复"
    if current == "frozen":
        return "R4.2事件解冻"
    if target == "invalidated":
        if "止损" in reason:
            return "R3.1收盘跌破Z"
        if "买点" in reason:
            return "R3.3回踩失败"
        if "退潮" in reason:
            return "R3.4环境前提消失"
    return "R3.0状态迁移"


class InMemorySignalEventStore:
    """内存事件存储（回测/单测用：进程内生命周期去重）"""

    def __init__(self):
        self.events: Dict[str, SignalEvent] = {}
        self.logs: List[Dict] = []

    def get_active_events(self, stock_code: str, entry_type: Optional[str] = None) -> List[SignalEvent]:
        return [
            e for e in self.events.values()
            if e.stock_code == stock_code and normalize_status(e.status) in ACTIVE_EVENT_STATUSES
            and (entry_type is None or e.entry_type == entry_type)
        ]

    def get_prior_events(self, stock_code: str) -> List[SignalEvent]:
        """【P1-2】历史事件（非活跃，含 triggered/invalidated/expired），
        按诞生日期排序——再入场次数与待命判定的数据源。"""
        prior = [
            e for e in self.events.values()
            if e.stock_code == stock_code
            and normalize_status(e.status) not in ("valid", "frozen")
        ]
        return sorted(prior, key=lambda e: e.born_date)

    def get_frozen_events(self) -> List[SignalEvent]:
        return [
            e for e in self.events.values()
            if normalize_status(e.status) == "frozen"
        ]

    def save(self, event: SignalEvent) -> None:
        event.status = normalize_status(event.status)
        previous = self.events.get(event.event_id)
        from_status = previous.status if previous else "none"
        if previous and not can_transition_status(previous.status, event.status):
            return
        if previous is None or previous.status != event.status:
            self.logs.append({
                "event_id": event.event_id,
                "stock_code": event.stock_code,
                "from_status": from_status,
                "to_status": event.status,
                "reason": event.invalid_reason,
                "source": "signal_lifecycle",
                "trigger_data": {},
                "rule_entry": _default_rule_entry(from_status, event.status),
                "rules_version": event.rules_version,
                "entry_snapshot": event.entry_snapshot,
                "maintenance_check": event.maintenance_check,
            })
        self.events[event.event_id] = event

    def update_status(
        self, event_id: str, status: str, reason: str = "",
        trigger_data: Optional[Dict] = None, rule_entry: str = "",
    ) -> None:
        event = self.events.get(event_id)
        if event:
            if not can_transition_status(event.status, status):
                return
            from_status = event.status
            event.status = normalize_status(status)
            event.invalid_reason = reason
            if event.status == "frozen":
                event.frozen_prev_status = from_status
            elif event.status == "pending_confirm":
                event.frozen_prev_status = from_status
            elif from_status in ("frozen", "pending_confirm"):
                event.frozen_prev_status = ""
            self.logs.append({
                "event_id": event_id,
                "stock_code": event.stock_code,
                "from_status": from_status,
                "to_status": event.status,
                "reason": reason,
                "source": "signal_lifecycle",
                "trigger_data": trigger_data or {},
                "rule_entry": rule_entry or _default_rule_entry(from_status, event.status),
                "rules_version": event.rules_version,
            })

    def refresh_frozen_reason(
        self, event_id: str, reason: str = "",
        trigger_data: Optional[Dict] = None, rule_entry: str = "",
    ) -> None:
        """P0-2：冻结原因随快照滚动更新——不改变状态、不触碰 frozen_prev_status。
        冻结持续期间每次评估刷新 invalid_reason 并留痕（from=frozen->to=frozen）。"""
        event = self.events.get(event_id)
        if not event or event.status != "frozen":
            return
        event.invalid_reason = reason
        self.logs.append({
            "event_id": event_id,
            "stock_code": event.stock_code,
            "from_status": "frozen",
            "to_status": "frozen",
            "reason": reason,
            "source": "signal_lifecycle",
            "trigger_data": trigger_data or {},
            "rule_entry": rule_entry or "",
            "rules_version": event.rules_version,
        })


    def get_event_logs(self, event_id: str) -> List[Dict]:
        return [log for log in self.logs if log["event_id"] == event_id]


class DbSignalEventStore:
    """SQLite 事件存储（实盘用：跨日生命周期）"""

    def _row_to_event(self, row) -> SignalEvent:
        return SignalEvent(
            event_id=row["event_id"],
            stock_code=row["stock_code"],
            stock_name=row["stock_name"] or "",
            entry_type=row["entry_type"],
            born_date=row["born_date"],
            expire_date=row["expire_date"] or "",
            breakout_level=float(row["breakout_level"] or 0),
            entry_price=float(row["entry_price"] or 0),
            stop_loss=float(row["stop_loss"] or 0),
            target_low=float(row["target_low"] or 0),
            target_high=float(row["target_high"] or 0),
            hypothesis_x=row["hypothesis_x"] or "",
            hypothesis_y=row["hypothesis_y"] or "",
            hypothesis_z=row["hypothesis_z"] or "",
            hypothesis_w=row["hypothesis_w"] or "",
            status=normalize_status(row["status"] or "valid"),
            invalid_reason=row["invalid_reason"] or "",
            rule_version=(row["rule_version"] or "") if "rule_version" in row.keys() else "",
            rules_version=(row["rules_version"] or "") if "rules_version" in row.keys() else "",
            frozen_prev_status=(row["frozen_prev_status"] or "") if "frozen_prev_status" in row.keys() else "",
            entry_snapshot=(row["entry_snapshot"] or "{}") if "entry_snapshot" in row.keys() else "{}",
            maintenance_check=(row["maintenance_check"] or "{}") if "maintenance_check" in row.keys() else "{}",
            y_formula=(row["y_formula"] or "") if "y_formula" in row.keys() else "",
            y_inputs=(row["y_inputs"] or "") if "y_inputs" in row.keys() else "",
        )

    def get_active_events(self, stock_code: str, entry_type: Optional[str] = None) -> List[SignalEvent]:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                if entry_type:
                    cursor.execute(
                        "SELECT * FROM signal_events WHERE stock_code=? "
                        "AND status IN ('valid','filled','frozen','pending_confirm','triggered') "
                        "AND entry_type=?",
                        (stock_code, entry_type),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM signal_events WHERE stock_code=? "
                        "AND status IN ('valid','filled','frozen','pending_confirm','triggered')",
                        (stock_code,),
                    )
                return [self._row_to_event(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("读取信号事件失败 %s: %s", stock_code, e)
            return []

    def get_prior_events(self, stock_code: str) -> List[SignalEvent]:
        """【P1-2】历史事件（非活跃，含 triggered/invalidated/expired），
        按诞生日期排序——再入场次数与待命判定的数据源。"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM signal_events WHERE stock_code=? "
                    "AND status NOT IN ('valid','frozen') "
                    "ORDER BY born_date",
                    (stock_code,),
                )
                return [self._row_to_event(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("读取历史信号事件失败 %s: %s", stock_code, e)
            return []

    def get_frozen_events(self) -> List[SignalEvent]:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM signal_events WHERE status='frozen' "
                    "ORDER BY born_date",
                )
                return [self._row_to_event(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("读取冻结信号事件失败: %s", e)
            return []
        except Exception as e:
            logger.error("读取历史信号事件失败 %s: %s", stock_code, e)
            return []

    def save(self, event: SignalEvent) -> None:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT status FROM signal_events WHERE event_id=?",
                    (event.event_id,),
                )
                existing = cursor.fetchone()
                from_status = existing["status"] if existing else "none"
                if existing and not can_transition_status(existing["status"], event.status):
                    conn.commit()
                    return False
                event.status = normalize_status(event.status)
                cursor.execute("""
                    INSERT OR REPLACE INTO signal_events
                    (event_id, stock_code, stock_name, entry_type, born_date, expire_date,
                     breakout_level, entry_price, stop_loss, target_low, target_high,
                     hypothesis_x, hypothesis_y, hypothesis_z, hypothesis_w,
                     status, invalid_reason, rule_version, rules_version, frozen_prev_status,
                     entry_snapshot, maintenance_check, y_formula, y_inputs, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.event_id, event.stock_code, event.stock_name, event.entry_type,
                    event.born_date, event.expire_date, event.breakout_level, event.entry_price,
                    event.stop_loss, event.target_low, event.target_high,
                    event.hypothesis_x, event.hypothesis_y, event.hypothesis_z, event.hypothesis_w,
                    event.status, event.invalid_reason, event.rule_version, event.rules_version,
                    event.frozen_prev_status,
                    event.entry_snapshot, event.maintenance_check,
                    event.y_formula, event.y_inputs,
                    datetime.now().isoformat(timespec="seconds"),
                ))
                if existing is None or from_status != event.status:
                    cursor.execute("""
                        INSERT INTO signal_event_logs
                        (event_id, stock_code, from_status, to_status, reason, source,
                         trigger_data, rule_entry, rules_version)
                        VALUES (?, ?, ?, ?, ?, 'signal_lifecycle', ?, ?, ?)
                    """, (
                        event.event_id, event.stock_code, from_status,
                        event.status, event.invalid_reason,
                        json.dumps({}, ensure_ascii=False, sort_keys=True),
                        _default_rule_entry(from_status, event.status),
                        event.rules_version,
                    ))
                conn.commit()
        except Exception as e:
            logger.error("写入信号事件失败 %s: %s", event.stock_code, e)

    def update_status(
        self, event_id: str, status: str, reason: str = "",
        trigger_data: Optional[Dict] = None, rule_entry: str = "",
    ) -> None:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT stock_code, status, rules_version, frozen_prev_status FROM signal_events WHERE event_id=?",
                    (event_id,),
                )
                existing = cursor.fetchone()
                if not existing or not can_transition_status(existing["status"], status):
                    conn.commit()
                    return False
                current = normalize_status(existing["status"])
                target = normalize_status(status)
                if target in ("frozen", "pending_confirm"):
                    cursor.execute(
                        "UPDATE signal_events SET status=?, invalid_reason=?, "
                        "frozen_prev_status=?, updated_at=? WHERE event_id=?",
                        (target, reason, current,
                         datetime.now().isoformat(timespec="seconds"), event_id),
                    )
                elif current in ("frozen", "pending_confirm"):
                    cursor.execute(
                        "UPDATE signal_events SET status=?, invalid_reason=?, "
                        "frozen_prev_status='', updated_at=? WHERE event_id=?",
                        (target, reason,
                         datetime.now().isoformat(timespec="seconds"), event_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE signal_events SET status=?, invalid_reason=?, updated_at=? WHERE event_id=?",
                        (target, reason, datetime.now().isoformat(timespec="seconds"), event_id),
                    )
                if current != target:
                    cursor.execute("""
                        INSERT INTO signal_event_logs
                        (event_id, stock_code, from_status, to_status, reason, source,
                         trigger_data, rule_entry, rules_version)
                        VALUES (?, ?, ?, ?, ?, 'signal_lifecycle', ?, ?, ?)
                    """, (
                        event_id, existing["stock_code"],
                        current, target, reason,
                        json.dumps(trigger_data or {}, ensure_ascii=False, sort_keys=True),
                        rule_entry or _default_rule_entry(current, target, reason),
                        (existing["rules_version"] or "") if "rules_version" in existing.keys() else "",
                    ))
                conn.commit()
        except Exception as e:
            logger.error("更新信号事件失败 %s: %s", event_id, e)

    def refresh_frozen_reason(
        self, event_id: str, reason: str = "",
        trigger_data: Optional[Dict] = None, rule_entry: str = "",
    ) -> None:
        """P0-2：冻结原因随快照滚动更新——不改变状态、不触碰 frozen_prev_status。
        冻结持续期间每次评估刷新 invalid_reason/updated_at 并留痕。"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT stock_code, status, rules_version FROM signal_events WHERE event_id=?",
                    (event_id,),
                )
                existing = cursor.fetchone()
                if not existing or existing["status"] != "frozen":
                    conn.commit()
                    return
                cursor.execute(
                    "UPDATE signal_events SET invalid_reason=?, updated_at=? WHERE event_id=?",
                    (reason, datetime.now().isoformat(timespec="seconds"), event_id),
                )
                cursor.execute("""
                    INSERT INTO signal_event_logs
                    (event_id, stock_code, from_status, to_status, reason, source,
                     trigger_data, rule_entry, rules_version)
                    VALUES (?, ?, 'frozen', 'frozen', ?, 'signal_lifecycle', ?, ?, ?)
                """, (
                    event_id, existing["stock_code"], reason,
                    json.dumps(trigger_data or {}, ensure_ascii=False, sort_keys=True),
                    rule_entry or "",
                    (existing["rules_version"] or "") if "rules_version" in existing.keys() else "",
                ))
                conn.commit()
        except Exception as e:
            logger.error("刷新冻结原因失败 %s: %s", event_id, e)

    def get_event_logs(self, event_id: str) -> List[Dict]:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM signal_event_logs WHERE event_id=? ORDER BY id",
                    (event_id,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("读取信号事件日志失败 %s: %s", event_id, e)
            return []


class SignalLifecycle:
    """生命周期编排：诞生 / 有效期 / 失效 / 触发 / 评估"""

    def __init__(self, store, valid_days: int = 5):
        self.store = store
        self.valid_days = int(valid_days)

    # ---------- 查询 ----------

    def has_active_event(self, stock_code: str, entry_type: Optional[str] = None) -> bool:
        return bool(self.get_active_events(stock_code, entry_type))

    def get_active_events(self, stock_code: str, entry_type: Optional[str] = None) -> List[SignalEvent]:
        return self.store.get_active_events(stock_code, entry_type)

    def get_prior_events(self, stock_code: str) -> List[SignalEvent]:
        return self.store.get_prior_events(stock_code)

    # ---------- 诞生 ----------

    def register_event(
        self,
        stock_code: str,
        stock_name: str,
        entry_type: str,
        breakout_level: float,
        entry_price: float,
        stop_loss: float,
        target_low: float,
        target_high: float,
        hypothesis: Optional[Dict] = None,
        event_id: str = "",
        born: Optional[date] = None,
        rule_version: str = "",
        y_formula: str = "",
        y_inputs: Optional[str | Dict] = None,
        rules_version: str = "",
        entry_snapshot: Optional[Dict] = None,
    ) -> SignalEvent:
        hyp = hypothesis or {}
        born = born or date.today()
        event = SignalEvent(
            event_id=event_id or f"evt-{datetime.now().strftime('%Y%m%d%H%M%S')}-{stock_code}-{entry_type}",
            stock_code=stock_code,
            stock_name=stock_name or "",
            entry_type=entry_type,
            born_date=born.isoformat(),
            expire_date=(born + timedelta(days=self.valid_days)).isoformat(),
            breakout_level=float(breakout_level or 0),
            entry_price=float(entry_price or 0),
            stop_loss=float(stop_loss or 0),
            target_low=float(target_low or 0),
            target_high=float(target_high or 0),
            hypothesis_x=str(hyp.get("x", "")),
            hypothesis_y=str(hyp.get("y", "")) + (f"（{hyp.get('y_note', '')}）" if hyp.get("y_note") else ""),
            hypothesis_z=f"{hyp.get('z_note', '')} Z={hyp.get('z', 0)}",
            hypothesis_w=f"{hyp.get('w_note', '')} W={hyp.get('w', [])}",
            rule_version=str(rule_version or ""),
            rules_version=str(rules_version or get_rules_version()),
            entry_snapshot=json.dumps(entry_snapshot or {}, ensure_ascii=False, sort_keys=True),
            y_formula=str(y_formula or ""),
            y_inputs=(
                y_inputs if isinstance(y_inputs, str)
                else json.dumps(y_inputs or {}, ensure_ascii=False, sort_keys=True)
            ),
        )
        self.store.save(event)
        logger.info(
            "信号事件诞生: %s %s %s 突破位%.2f 买点%.2f Z%.2f 有效期至%s",
            event.event_id, stock_code, entry_type,
            event.breakout_level, event.entry_price, event.stop_loss, event.expire_date,
        )
        return event

    # ---------- 状态迁移 ----------

    def mark_filled(
        self, event_id: str, reason: str = "价格触及买点，转入成交跟踪",
        trigger_data: Optional[Dict] = None,
    ) -> None:
        self.store.update_status(
            event_id, "filled", reason,
            trigger_data=trigger_data, rule_entry="R3.2回踩确认",
        )

    def mark_triggered(self, event_id: str) -> None:
        """兼容旧调用：旧 triggered 语义等同于 filled。"""
        self.store.update_status(event_id, "filled", "价格触及买点，转入成交跟踪")

    def invalidate(
        self, event_id: str, reason: str,
        trigger_data: Optional[Dict] = None,
    ) -> None:
        self.store.update_status(
            event_id, "invalidated", reason,
            trigger_data=trigger_data,
        )

    def expire(self, event_id: str, trigger_data: Optional[Dict] = None) -> None:
        self.store.update_status(
            event_id, "expired", "回踩买点有效期超期作废",
            trigger_data=trigger_data, rule_entry="R3.5有效期到期",
        )

    def mark_chase_abandon(self, event_id: str, trigger_data: Optional[Dict] = None) -> None:
        self.store.update_status(
            event_id, "chase_abandon", "价格远离买点，回踩假设失效",
            trigger_data=trigger_data, rule_entry="R3.6追高放弃",
        )

    def mark_signal_target(self, event_id: str, trigger_data: Optional[Dict] = None) -> None:
        self.store.update_status(
            event_id, "sig_target", "先触及目标价，信号口径赢",
            trigger_data=trigger_data, rule_entry="R3.7信号止盈",
        )

    def mark_signal_stop(self, event_id: str, trigger_data: Optional[Dict] = None) -> None:
        self.store.update_status(
            event_id, "sig_stop", "先触及止损价，信号口径输",
            trigger_data=trigger_data, rule_entry="R3.8信号止损",
        )

    def mark_time_exit(self, event_id: str, trigger_data: Optional[Dict] = None) -> None:
        self.store.update_status(
            event_id, "time_exit", "到期未触目标/止损，信号口径平",
            trigger_data=trigger_data, rule_entry="R3.9时间离场",
        )

    def set_maintenance_check(self, stock_code: str, data: Dict) -> None:
        payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
        for event in self.get_active_events(stock_code):
            event.maintenance_check = payload
            self.store.save(event)

    def record_daily_snapshot(
        self, event_id: str, snapshot_date: str,
        close_price: Optional[float], status: str,
    ) -> bool:
        from ..feedback.signal_ledger import record_daily_snapshot
        return record_daily_snapshot(event_id, snapshot_date, close_price, status)

    def freeze(
        self, event_id: str, reason: str,
        trigger_data: Optional[Dict] = None,
    ) -> None:
        self.store.update_status(
            event_id, "frozen", reason,
            trigger_data=trigger_data, rule_entry="R4.1事件冻结",
        )

    def unfreeze(
        self, event_id: str, target_status: str = "valid",
        trigger_data: Optional[Dict] = None,
    ) -> None:
        self.store.update_status(
            event_id, target_status,
            "技术分回升，冻结解除",
            trigger_data=trigger_data, rule_entry="R4.2事件解冻",
        )

    def apply_projection_recheck(
        self, actual_ratios: Dict[str, float], trade_date: Optional[date] = None,
    ) -> List[Dict]:
        """盘后复核：外推触发的活跃事件在实际量能严重不足时转待确认。"""
        trade_date = trade_date or date.today()
        notices: List[Dict] = []
        for code, raw_actual in (actual_ratios or {}).items():
            try:
                actual_ratio = float(raw_actual)
            except (TypeError, ValueError):
                continue
            if actual_ratio <= 0:
                continue
            for event in self.get_active_events(code):
                try:
                    snapshot = json.loads(event.entry_snapshot or "{}")
                except Exception:
                    snapshot = {}
                if not snapshot.get("projection_triggered"):
                    continue
                try:
                    threshold = float(snapshot.get("projection_threshold") or 1.2)
                except (TypeError, ValueError):
                    threshold = 1.2
                if actual_ratio >= threshold * 0.8:
                    continue
                reason = (
                    f"外推量能盘后未确认: 实际{actual_ratio:.2f}x < "
                    f"触发阈值{threshold:.2f}x的80%，转入待确认"
                )
                self.store.update_status(
                    event.event_id, "pending_confirm", reason,
                    trigger_data={
                        "actual_ratio": actual_ratio,
                        "projection_threshold": threshold,
                        "trade_date": trade_date.isoformat(),
                    },
                )
                notices.append({
                    "stock_code": code,
                    "stock_name": event.stock_name,
                    "exit_type": "外推量能待确认",
                    "trigger_price": event.entry_price,
                    "stop_loss_price": event.stop_loss,
                    "reason": f"[{event.entry_type}] {reason}",
                    "urgency": "重要",
                    "event_id": event.event_id,
                })
        return notices

    # ---------- 每轮评估 ----------

    def evaluate_events(
        self,
        stock_code: str,
        current_price: float,
        sector_status: str = "",
        today: Optional[date] = None,
        day_high: Optional[float] = None,
        day_low: Optional[float] = None,
        close_price: Optional[float] = None,
        exchange_close: Optional[float] = None,
        tech_score: Optional[float] = None,
        volume_ratio: Optional[float] = None,
        change_pct: Optional[float] = None,
    ) -> List[Dict]:
        """
        评估该股全部活跃事件，返回需要推送的状态迁移通知（dict 出场信号）。

        回踩状态机：
          - 现价跌破止损线：结构失败；
          - 现价跌破买点：回踩失败，未成交买单立即撤单；
          - 日内最低价触及买点且现价守住买点：已成交。

        失效（立即撤单）：
          - 收盘跌回突破位（买点缺失时回退判定）：current < breakout_level
          - 板块状态机转为退潮：sector_status == 'retreating'
        过期（静默作废 + 常规通知）：
          - today > expire_date
        """
        today = today or date.today()
        notices: List[Dict] = []
        for event in self.get_active_events(stock_code):
            close = float(close_price) if close_price is not None else None
            try:
                exchange_close_f = float(exchange_close) if exchange_close is not None else None
            except (TypeError, ValueError):
                exchange_close_f = None
            low = float(day_low) if day_low is not None else None
            high = float(day_high) if day_high is not None else None
            effective_price = close if close is not None else current_price
            # P2-14 完结价口径：close 与交易所收盘一致（或未知时存在 close）才算收盘，
            # 否则为现价快照——完结价必须带口径，差异>0.1% 时渲染层双值显示。
            price_source = "收盘"
            if close is not None and exchange_close_f is not None:
                if abs(close - exchange_close_f) > max(0.01, abs(exchange_close_f) * 0.001):
                    price_source = "现价快照"
            elif close is None:
                price_source = "现价快照"

            def _notice(exit_type: str, reason: str, urgency: str) -> Dict:
                return {
                    "stock_code": stock_code,
                    "stock_name": event.stock_name,
                    "exit_type": exit_type,
                    "trigger_price": effective_price,
                    "stop_loss_price": event.stop_loss,
                    "reason": f"[{event.entry_type}] {reason}",
                    "urgency": urgency,
                    "sector_status": sector_status,
                    "event_id": event.event_id,
                }

            if close is not None:
                self.record_daily_snapshot(
                    event.event_id, today.isoformat(), close, event.status,
                )

            try:
                chase_gap = (
                    (float(effective_price) / float(event.entry_price) - 1.0)
                    if effective_price and event.entry_price else None
                )
            except (TypeError, ValueError):
                chase_gap = None
            if (
                event.status in ("valid", "frozen")
                and chase_gap is not None and chase_gap > 0.15
            ):
                reason = (
                    f"现价{float(effective_price):.2f}距买点"
                    f"{event.entry_price:.2f}超过+15%"
                    f"({chase_gap:+.1%})，回踩假设失效"
                )
                self.mark_chase_abandon(
                    event.event_id,
                    trigger_data={
                        "price": effective_price,
                        "entry_price": event.entry_price,
                        "chase_gap": chase_gap,
                        "today": today.isoformat(),
                        "price_source": price_source,
                        "exchange_close": exchange_close_f,
                    },
                )
                notices.append(_notice("追高放弃", reason, "常规"))
                continue

            if event.status == "pending_confirm":
                try:
                    next_day_volume = (
                        float(volume_ratio) if volume_ratio is not None else None
                    )
                except (TypeError, ValueError):
                    next_day_volume = None
                if next_day_volume is not None and next_day_volume < 1.0:
                    reason = (
                        f"外推量能盘后未确认，次日量比{next_day_volume:.2f}<1.0，"
                        "撤销回踩买入指令"
                    )
                    self.invalidate(
                        event.event_id, reason,
                        trigger_data={
                            "volume_ratio": next_day_volume,
                            "today": today.isoformat(),
                            "rule": "外推量能次日复核",
                        },
                    )
                    notices.append(_notice("信号作废", reason, "重要"))
                elif next_day_volume is not None:
                    target = event.frozen_prev_status or "valid"
                    if target not in ("valid", "filled"):
                        target = "valid"
                    reason = (
                        f"外推量能次日复核通过，量比{next_day_volume:.2f}≥1.0，"
                        "恢复原状态"
                    )
                    self.store.update_status(
                        event.event_id, target, reason,
                        trigger_data={
                            "volume_ratio": next_day_volume,
                            "today": today.isoformat(),
                            "rule": "外推量能次日复核",
                        },
                        rule_entry="R3.12外推量能确认恢复",
                    )
                    notices.append(_notice("外推量能确认", reason, "常规"))
                continue

            if event.status == "frozen":
                if event.expire_date and today.isoformat() > event.expire_date:
                    self.expire(
                        event.event_id,
                        trigger_data={
                            "today": today.isoformat(),
                            "expire_date": event.expire_date,
                            "close": effective_price,
                            "price_source": price_source,
                            "exchange_close": exchange_close_f,
                        },
                    )
                    notices.append(_notice("信号过期", "冻结事件超过有效期，自动过期", "常规"))
                    continue
                if tech_score is not None and float(tech_score) >= 0:
                    target = event.frozen_prev_status or "valid"
                    self.unfreeze(
                        event.event_id, target,
                        trigger_data={
                            "tech_score": tech_score,
                            "price": effective_price,
                            "today": today.isoformat(),
                        },
                    )
                    notices.append(_notice(
                        "事件解冻",
                        f"技术分{float(tech_score):+.1f}≥0，恢复原状态",
                        "常规",
                    ))
                else:
                    # P0-2：冻结原因随快照滚动更新——冻结持续期间 invalid_reason
                    # 刷新为最新快照，禁止展示两个版本前的旧原因（沃尔德 9-18）。
                    if tech_score is not None and volume_ratio is not None and change_pct is not None:
                        _refresh_reason = (
                            f"技术{float(tech_score):+.1f}"
                            f"（放量阴线{float(change_pct):+.2f}%，"
                            f"量比{float(volume_ratio):.2f}）；"
                            "解冻条件技术分≥0，冻结期间不提示回踩买入；"
                            "冻结仅停提示、挂单存续（解冻后恢复可成交）"
                        )
                        self.store.refresh_frozen_reason(
                            event.event_id, _refresh_reason,
                            trigger_data={
                                "tech_score": tech_score,
                                "volume_ratio": volume_ratio,
                                "change_pct": change_pct,
                                "price": effective_price,
                                "today": today.isoformat(),
                            },
                            rule_entry="R3.11冻结快照滚动",
                        )
                continue
            if event.status != "valid":
                # filled 后仍按信号口径跟踪 W/Z；不会读持仓或成本。
                if event.status == "filled":
                    stop = float(event.stop_loss or 0)
                    target = float(event.target_low or event.target_high or 0)
                    price = float(effective_price or 0)
                    low_value = float(low) if low is not None else None
                    high_value = float(high) if high is not None else None
                    hit_stop = bool(
                        stop > 0 and ((low_value is not None and low_value <= stop)
                                      or (price > 0 and price <= stop))
                    )
                    hit_target = bool(
                        target > 0 and ((high_value is not None and high_value >= target)
                                        or (price > 0 and price >= target))
                    )
                    if hit_stop:
                        reason = f"先触及止损{stop:.2f}，信号口径认错"
                        self.mark_signal_stop(
                            event.event_id,
                        trigger_data={
                            "close": effective_price, "day_low": low,
                            "day_high": high, "stop_loss": stop,
                            "today": today.isoformat(),
                            "price_source": price_source,
                            "exchange_close": exchange_close_f,
                        },
                        )
                        notices.append(_notice("信号止损", reason, "重要"))
                        continue
                    if hit_target:
                        reason = f"先触及目标{target:.2f}，信号口径兑现"
                        self.mark_signal_target(
                            event.event_id,
                        trigger_data={
                            "close": effective_price, "day_low": low,
                            "day_high": high, "target": target,
                            "today": today.isoformat(),
                            "price_source": price_source,
                            "exchange_close": exchange_close_f,
                        },
                        )
                        notices.append(_notice("信号止盈", reason, "常规"))
                        continue
                    if event.expire_date and today.isoformat() > event.expire_date:
                        self.mark_time_exit(
                            event.event_id,
                        trigger_data={
                            "close": effective_price,
                            "expire_date": event.expire_date,
                            "today": today.isoformat(),
                            "price_source": price_source,
                            "exchange_close": exchange_close_f,
                        },
                        )
                        notices.append(_notice("时间离场", "有效期内未触目标或止损", "常规"))
                        continue
                continue

            try:
                should_freeze = (
                    tech_score is not None
                    and float(tech_score) <= -1
                    and volume_ratio is not None
                    and float(volume_ratio) >= 1.2
                    and change_pct is not None
                    and float(change_pct) < 0
                )
            except (TypeError, ValueError):
                should_freeze = False
            if should_freeze:
                reason = (
                    f"技术{float(tech_score):+.1f}"
                    f"（放量阴线{float(change_pct):+.2f}%，"
                    f"量比{float(volume_ratio):.2f}）；"
                    "解冻条件技术分≥0，冻结期间不提示回踩买入；"
                    "冻结仅停提示、挂单存续（解冻后恢复可成交）"
                )
                self.freeze(
                    event.event_id, reason,
                    trigger_data={
                        "tech_score": tech_score,
                        "volume_ratio": volume_ratio,
                        "change_pct": change_pct,
                        "price": effective_price,
                        "today": today.isoformat(),
                    },
                )
                notices.append(_notice("事件冻结", reason, "重要"))
                continue
            # 收盘规则优先级：止损是结构死亡 > 买点撤单 > 日内触及成交。
            if close is not None and event.stop_loss and close < event.stop_loss:
                reason = (
                    f"收盘{close:.2f}跌破止损线{event.stop_loss:.2f}，"
                    "结构失败，买单撤单"
                )
                self.invalidate(
                    event.event_id, reason,
                    trigger_data={
                        "close": close, "stop_loss": event.stop_loss,
                        "today": today.isoformat(),
                        "price_source": price_source,
                        "exchange_close": exchange_close_f,
                    },
                )
                notices.append(_notice("信号作废", reason, "紧急"))
                continue

            failure_anchor = event.entry_price or event.breakout_level
            effective_current = close if close is not None else current_price
            if effective_current and failure_anchor and effective_current < failure_anchor:
                touched = low is not None and low <= failure_anchor
                anchor_text = (
                    f"{event.entry_price:.2f}" if event.entry_price
                    else f"{event.breakout_level:.2f}"
                )
                reason = (
                    f"现价{effective_current:.2f}跌破买点{anchor_text}，"
                    "回踩失败已撤单"
                    + ("（盘中曾触及买点，不能回补）" if touched else "")
                )
                self.invalidate(
                    event.event_id, reason,
                    trigger_data={
                        "price": effective_current,
                        "failure_anchor": failure_anchor,
                        "day_low": low,
                        "today": today.isoformat(),
                        "price_source": price_source,
                        "exchange_close": exchange_close_f,
                    },
                )
                notices.append(_notice("信号作废", reason, "重要"))
                continue

            # 环境前提消失时不能先确认成交；回撤单优先于虚拟成交。
            if sector_status == "retreating":
                reason = "板块状态机转为退潮，假说环境前提消失"
                self.invalidate(
                    event.event_id, reason,
                    trigger_data={
                        "sector_status": sector_status,
                        "today": today.isoformat(),
                    },
                )
                notices.append(_notice("信号作废", reason + "——立即撤单", "重要"))
                continue

            if (
                low is not None
                and event.entry_price
                and low <= event.entry_price
                and (close is None or close >= event.entry_price)
            ):
                self.mark_filled(
                    event.event_id,
                    trigger_data={
                        "day_low": low,
                        "entry_price": event.entry_price,
                        "close": close,
                        "today": today.isoformat(),
                    },
                )
                close_text = f"，收盘{close:.2f}" if close is not None else ""
                reason = (
                    f"日内最低{low:.2f}触及买点{event.entry_price:.2f}"
                    f"{close_text}，虚拟成交"
                )
                notices.append(_notice("信号成交", reason, "重要"))
                continue

            if event.expire_date and today.isoformat() > event.expire_date:
                self.expire(
                    event.event_id,
                    trigger_data={
                        "today": today.isoformat(),
                        "expire_date": event.expire_date,
                        "close": close or current_price,
                        "price_source": price_source,
                        "exchange_close": exchange_close_f,
                    },
                )
                notices.append({
                    "stock_code": stock_code,
                    "stock_name": event.stock_name,
                    "exit_type": "信号过期",
                    "trigger_price": current_price,
                    "stop_loss_price": event.breakout_level,
                    "reason": f"[{event.entry_type}] 回踩买点{event.entry_price:.2f}"
                              f"有效期{self.valid_days}日已过，作废不再重播",
                    "urgency": "常规",
                    "sector_status": sector_status,
                    "event_id": event.event_id,
                })
        return notices

    def run_daily_unfreeze(
        self,
        tech_scores: Dict[str, float],
        today: Optional[date] = None,
    ) -> List[Dict]:
        """日终仲裁：只处理 FROZEN 解冻，报告层只读批处理后的状态。"""
        today = today or date.today()
        notices: List[Dict] = []
        for event in self.store.get_frozen_events():
            if event.expire_date and today.isoformat() > event.expire_date:
                self.expire(
                    event.event_id,
                    trigger_data={
                        "today": today.isoformat(),
                        "expire_date": event.expire_date,
                    },
                )
                notices.append({
                    "stock_code": event.stock_code,
                    "stock_name": event.stock_name,
                    "exit_type": "信号过期",
                    "reason": f"[{event.entry_type}] 冻结事件超过有效期，自动过期",
                    "urgency": "常规",
                    "event_id": event.event_id,
                })
                continue
            score = tech_scores.get(event.stock_code)
            if score is None or float(score) < 0:
                continue
            target = event.frozen_prev_status or "valid"
            self.unfreeze(
                event.event_id, target,
                trigger_data={"tech_score": float(score)},
            )
            notices.append({
                "stock_code": event.stock_code,
                "stock_name": event.stock_name,
                "exit_type": "事件解冻",
                "reason": (
                    f"[{event.entry_type}] 技术分{float(score):+.1f}≥0，"
                    "恢复原状态"
                ),
                "urgency": "常规",
                "event_id": event.event_id,
            })
        return notices

    def event_status_note(self, stock_code: str, current_price: float = 0,
                          current_rule_version: str = "") -> str:
        """观察卡用：活跃事件的状态描述（回踩买点是否仍有效）。

        【Phase5 回炉】事件带规则版本：与当前 z_line_mode 不同的
        存量事件标注“旧版规则”，双轨可审计；旧库事件（版本空）
        也标“旧版规则（版本未记录）”。
        """
        events = self.get_active_events(stock_code)
        if not events:
            prior = self.get_prior_events(stock_code)
            if not prior:
                return ""
            events = [prior[-1]]
        notes = []
        for e in events:
            age = ""
            try:
                born = date.fromisoformat(e.born_date)
                age = f"第{(date.today() - born).days + 1}天"
            except ValueError:
                pass
            if e.status == "frozen":
                notes.append(
                    f"[{e.entry_type}]已冻结：{e.invalid_reason or '评分仲裁触发'}"
                )
                continue
            if e.status == "filled":
                audit_note = render_entry_and_maintenance(e)
                notes.append(
                    f"[{e.entry_type}]已成交第{self._event_day(e)}天·"
                    f"持有检查{self._maintenance_health(e)}\n{audit_note}"
                )
                continue
            if e.status in TERMINAL_EVENT_STATUSES:
                completion_price, price_source, exchange_close = (
                    self._completion_price_detail(e)
                )
                # P2-14：完结价带口径（收盘/现价快照），与交易所收盘差异>0.1% 时双值显示。
                if completion_price > 0:
                    completion_text = f"{completion_price:.2f}({price_source})"
                    if (
                        exchange_close is not None
                        and exchange_close > 0
                    ):
                        _diff = abs(exchange_close - completion_price)
                        # P2-14：与交易所收盘差异>0.1元 或 >0.1%（中际旭创 908.09 vs 907.80）双值显示
                        if _diff > 0.1 or (_diff / exchange_close) > 0.001:
                            completion_text += f" vs 交易所收盘{exchange_close:.2f}"
                else:
                    completion_text = "数据未取到"
                distance = ""
                if completion_price and e.entry_price:
                    distance = f" 距买点{(completion_price / e.entry_price - 1.0):+.1%}"
                notes.append(
                    f"[{e.entry_type}]已完结({completion_reason(e.status)})："
                    f"完结价{completion_text}"
                    f"{distance}；原因{e.invalid_reason or '规则迁移'}"
                )
                continue
            pullback_ok = "回踩买点有效" if (not current_price or current_price <= e.entry_price * 1.01) else "买点上方待回踩"
            if e.rule_version and current_rule_version and e.rule_version != current_rule_version:
                ver_note = f",Z按{display_rule_version(e.rule_version)}(旧版)"
            elif not e.rule_version:
                ver_note = ",Z按旧版规则(版本未记录)"
            elif e.rule_version:
                ver_note = f",Z规则:{display_rule_version(e.rule_version)}"
            else:
                ver_note = ""
            global_version = str(getattr(e, "rules_version", "") or "")
            global_ver_note = (
                f",规则:{global_version}" if global_version else ",规则未记录"
            )
            audit_note = render_entry_and_maintenance(e)
            notes.append(
                f"[{e.entry_type}]事件{age}{pullback_ok}"
                f"(Y={e.entry_price:.2f},Z={e.stop_loss:.2f}{ver_note},"
                f"{global_ver_note.lstrip(',')},有效至{e.expire_date})"
                f"\n{audit_note}"
            )
        return "；".join(notes)

    def _event_day(self, event) -> int:
        try:
            return (date.today() - date.fromisoformat(event.born_date)).days + 1
        except (TypeError, ValueError):
            return 0

    def _maintenance_health(self, event) -> str:
        try:
            maintenance = json.loads(event.maintenance_check or "{}")
        except Exception:
            maintenance = {}
        flags = _snapshot_flags(maintenance, maintenance=True)
        if not flags:
            return "未评估"
        return "走弱" if any("△" in flag for flag in flags) else "正常"

    def _completion_price(self, event) -> float:
        return self._completion_price_detail(event)[0]

    def _completion_price_detail(self, event):
        """完结价 + 口径（收盘/现价快照）+ 交易所收盘。旧事件无口径字段时按键名推断。"""
        try:
            log = self.store.get_event_logs(event.event_id)[-1]
            raw_data = log.get("trigger_data") or {}
            data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        except Exception:
            return 0.0, "数据未取到", None
        for key in ("price", "close"):
            try:
                value = float(data.get(key))
                if value > 0:
                    source = str(data.get("price_source") or "").strip()
                    if not source:
                        source = "收盘" if key == "close" else "现价快照"
                    exchange_close = None
                    try:
                        ec = float(data.get("exchange_close"))
                        if ec > 0:
                            exchange_close = ec
                    except (TypeError, ValueError):
                        pass
                    return value, source, exchange_close
            except (TypeError, ValueError):
                continue
        return 0.0, "数据未取到", None


# ============================================================
# 【Phase4 P1-2】再入场循环 — 止损是尝试的结束，不是死刑判决
# ============================================================
# 趋势跟踪的盈利结构依赖“小止损+再入场+让利润奔跑”
# （蘅东光 9/4 止损 -5%、9/7 +14.8%，缺的只是第二次入场）。
#
# 纪律（反方对冲，必须写进实现）：
#   - 再入场必须是新事件（创新高或收复 Z 线，且重新通过完整触发条件），
#     绝不基于“已经亏了所以补回”的沉没成本逻辑；
#   - 同一标的再入场次数设上限（默认 2 次）。
# 作废条件：若统计显示再入场的期望值显著低于首入场
# （二次突破失败率 >70%），收缩为仅创新高可再入。
# ============================================================

def reentry_exhausted(stock_code: str, max_reentries: int = 2, store=None) -> bool:
    """再入场次数是否用尽（真实失败终态 ≥ 上限 → 转入长期观察）。

    triggered 是已成交事件，不能算“已结束尝试”；expired 是未成交，
    也不具备“止损/失效后再走强”的再入场语义。
    """
    prior = (store or InMemorySignalEventStore()).get_prior_events(stock_code)
    return len([e for e in prior if e.status in TERMINAL_REENTRY_STATUSES]) >= max_reentries


def reentry_status(
    stock_code: str,
    current_price: float = 0,
    recent_high: float = 0,
    max_reentries: int = 2,
    new_high_ratio: float = 0.99,
    store=None,
    prior_high: float = 0,
) -> Dict:
    """再入场待命判定（观察卡/待命队列用）。

    【Phase5 回炉】创新高判定锚 prior_high（近20日高剔除当日，
    参数为 0 时回退 recent_high）：盘中创新高但收盘回落 >1% 的
    标的（蘅东光 9/7 类）不再被“含当日高点”口径误拦。

    返回 {
      "standby": bool,          # 是否进入再入场待命队列
      "reason": str,            # 创新高 / 收复Z线 / 次数用尽 / 无历史事件
      "attempts": int,          # 已用尝试次数（历史 triggered/invalidated 数）
      "remaining": int,         # 剩余再入场次数
      "note": str,              # 展示文案
    }
    """
    prior = (store or InMemorySignalEventStore()).get_prior_events(stock_code)

    # 写入口唯一：任何活跃事件都由事件库跟踪，队列不得覆盖或复制它。
    active = (store or InMemorySignalEventStore()).get_active_events(stock_code)
    if active:
        last = active[-1]
        active_label = display_status(last.status)
        return {
            "standby": False,
            "reason": f"{active_label}事件在跟踪",
            "event_id": last.event_id,
            "attempts": 0,
            "remaining": max_reentries,
            "note": (
                f"{active_label}事件{last.event_id}仍在跟踪（{last.entry_type} "
                f"{last.born_date}）；执行系统按事件ID对号入座，"
                "不进入再入场队列"
            ),
        }

    # 再入场只服务止损/失效终态；triggered/expired 不是可再入语义。
    attempts = len([e for e in prior if e.status in TERMINAL_REENTRY_STATUSES])
    remaining = max(0, max_reentries - attempts)

    if not prior or attempts == 0:
        return {
            "standby": False,
            "reason": "无已了结尝试" if prior else "无历史事件",
            "attempts": attempts,
            "remaining": remaining,
            "note": (
                "历史事件均为未入场形态（过期/撤单），无尝试可再入；"
                "正常信号流程覆盖该标的" if prior else ""
            ),
        }
    if remaining <= 0:
        terminal_events = [e for e in prior if e.status in TERMINAL_REENTRY_STATUSES]
        last = terminal_events[-1]
        return {
            "standby": False, "reason": "次数用尽", "attempts": attempts,
            "event_id": last.event_id if attempts else "",
            "remaining": 0,
            "note": f"再入场{attempts}次已用尽（上限{max_reentries}），转入长期观察",
        }

    terminal_events = [e for e in prior if e.status in TERMINAL_REENTRY_STATUSES]
    last = terminal_events[-1]
    # 新事件判定一：创新高（锚前期高点，剔除当日）
    high_anchor = float(prior_high) if prior_high else float(recent_high)
    if current_price and high_anchor and current_price >= high_anchor * new_high_ratio:
        return {
            "standby": True, "reason": "创新高", "attempts": attempts,
            "event_id": last.event_id,
            "remaining": remaining,
            "note": (
                f"【再入场待命·头部】第{attempts}次尝试已结束("
                f"{last.entry_type} {last.born_date}，{display_status(last.status)})；"
                f"现价{current_price:.2f}创近段新高{high_anchor:.2f}——"
                "新事件成立，待完整触发条件重新确认后入场"
                "（不携带沉没成本逻辑）"
            ),
        }
    # 新事件判定二：收复 Z 线（价格重新站上上次认错位）
    if current_price and last.stop_loss and current_price > last.stop_loss:
        recovered = (current_price / last.stop_loss - 1) * 100
        return {
            "standby": True, "reason": "收复Z线", "attempts": attempts,
            "event_id": last.event_id,
            "remaining": remaining,
            "note": (
                f"【再入场待命】第{attempts}次尝试已结束("
                f"{last.entry_type} {last.born_date}，{display_status(last.status)})；"
                f"现价{current_price:.2f}已收复上次认错位{last.stop_loss:.2f}"
                f"(+{recovered:.1f}%)——结构修复，待完整触发条件重新确认"
            ),
        }
    return {
            "standby": False, "reason": "新事件未成立", "attempts": attempts,
            "event_id": last.event_id,
        "remaining": remaining,
        "note": (
            f"上次尝试{last.born_date}已结束({display_status(last.status)})；"
            "创新高/收复Z线均未成立，再入场条件未激活"
        ),
    }
