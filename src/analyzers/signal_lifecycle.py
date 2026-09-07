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
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from ..db import get_conn

logger = logging.getLogger(__name__)


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
    status: str = "valid"           # valid / triggered / expired / invalidated
    invalid_reason: str = ""


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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_code_status "
                   "ON signal_events(stock_code, status)")


class InMemorySignalEventStore:
    """内存事件存储（回测/单测用：进程内生命周期去重）"""

    def __init__(self):
        self.events: Dict[str, SignalEvent] = {}

    def get_active_events(self, stock_code: str, entry_type: Optional[str] = None) -> List[SignalEvent]:
        return [
            e for e in self.events.values()
            if e.stock_code == stock_code and e.status == "valid"
            and (entry_type is None or e.entry_type == entry_type)
        ]

    def save(self, event: SignalEvent) -> None:
        self.events[event.event_id] = event

    def update_status(self, event_id: str, status: str, reason: str = "") -> None:
        event = self.events.get(event_id)
        if event:
            event.status = status
            event.invalid_reason = reason


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
            status=row["status"] or "valid",
            invalid_reason=row["invalid_reason"] or "",
        )

    def get_active_events(self, stock_code: str, entry_type: Optional[str] = None) -> List[SignalEvent]:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                if entry_type:
                    cursor.execute(
                        "SELECT * FROM signal_events WHERE stock_code=? AND status='valid' AND entry_type=?",
                        (stock_code, entry_type),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM signal_events WHERE stock_code=? AND status='valid'",
                        (stock_code,),
                    )
                return [self._row_to_event(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("读取信号事件失败 %s: %s", stock_code, e)
            return []

    def save(self, event: SignalEvent) -> None:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute("""
                    INSERT OR REPLACE INTO signal_events
                    (event_id, stock_code, stock_name, entry_type, born_date, expire_date,
                     breakout_level, entry_price, stop_loss, target_low, target_high,
                     hypothesis_x, hypothesis_y, hypothesis_z, hypothesis_w,
                     status, invalid_reason, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.event_id, event.stock_code, event.stock_name, event.entry_type,
                    event.born_date, event.expire_date, event.breakout_level, event.entry_price,
                    event.stop_loss, event.target_low, event.target_high,
                    event.hypothesis_x, event.hypothesis_y, event.hypothesis_z, event.hypothesis_w,
                    event.status, event.invalid_reason, datetime.now().isoformat(timespec="seconds"),
                ))
                conn.commit()
        except Exception as e:
            logger.error("写入信号事件失败 %s: %s", event.stock_code, e)

    def update_status(self, event_id: str, status: str, reason: str = "") -> None:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                _ensure_tables(cursor)
                cursor.execute(
                    "UPDATE signal_events SET status=?, invalid_reason=?, updated_at=? WHERE event_id=?",
                    (status, reason, datetime.now().isoformat(timespec="seconds"), event_id),
                )
                conn.commit()
        except Exception as e:
            logger.error("更新信号事件失败 %s: %s", event_id, e)


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
        )
        self.store.save(event)
        logger.info(
            "信号事件诞生: %s %s %s 突破位%.2f 买点%.2f Z%.2f 有效期至%s",
            event.event_id, stock_code, entry_type,
            event.breakout_level, event.entry_price, event.stop_loss, event.expire_date,
        )
        return event

    # ---------- 状态迁移 ----------

    def mark_triggered(self, event_id: str) -> None:
        self.store.update_status(event_id, "triggered", "回执成交，转入持仓配对出场")

    def invalidate(self, event_id: str, reason: str) -> None:
        self.store.update_status(event_id, "invalidated", reason)

    def expire(self, event_id: str) -> None:
        self.store.update_status(event_id, "expired", "回踩买点有效期超期作废")

    # ---------- 每轮评估 ----------

    def evaluate_events(
        self,
        stock_code: str,
        current_price: float,
        sector_status: str = "",
        today: Optional[date] = None,
    ) -> List[Dict]:
        """
        评估该股全部活跃事件，返回需要推送的状态迁移通知（dict 出场信号）。

        失效（立即撤单）：
          - 收盘跌回突破位：current < breakout_level
          - 板块状态机转为退潮：sector_status == 'retreating'
        过期（静默作废 + 常规通知）：
          - today > expire_date
        """
        today = today or date.today()
        notices: List[Dict] = []
        for event in self.get_active_events(stock_code):
            if (current_price and event.breakout_level and
                    current_price < event.breakout_level):
                reason = (f"收盘/盘中价{current_price:.2f}跌回突破位"
                          f"{event.breakout_level:.2f}，假说被证伪")
                self.invalidate(event.event_id, reason)
                notices.append({
                    "stock_code": stock_code,
                    "stock_name": event.stock_name,
                    "exit_type": "信号作废",
                    "trigger_price": current_price,
                    "stop_loss_price": event.breakout_level,
                    "reason": f"[{event.entry_type}] {reason}——立即撤单，"
                              f"未成交买单不再有效",
                    "urgency": "重要",
                    "sector_status": sector_status,
                })
            elif sector_status == "retreating":
                reason = "板块状态机转为退潮，假说环境前提消失"
                self.invalidate(event.event_id, reason)
                notices.append({
                    "stock_code": stock_code,
                    "stock_name": event.stock_name,
                    "exit_type": "信号作废",
                    "trigger_price": current_price,
                    "stop_loss_price": event.breakout_level,
                    "reason": f"[{event.entry_type}] {reason}——立即撤单",
                    "urgency": "重要",
                    "sector_status": sector_status,
                })
            elif event.expire_date and today.isoformat() > event.expire_date:
                self.expire(event.event_id)
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
                })
        return notices

    def event_status_note(self, stock_code: str, current_price: float = 0) -> str:
        """观察卡用：活跃事件的状态描述（回踩买点是否仍有效）"""
        events = self.get_active_events(stock_code)
        if not events:
            return ""
        notes = []
        for e in events:
            age = ""
            try:
                born = date.fromisoformat(e.born_date)
                age = f"第{(date.today() - born).days + 1}天"
            except ValueError:
                pass
            pullback_ok = "回踩买点有效" if (not current_price or current_price <= e.entry_price * 1.01) else "买点上方待回踩"
            notes.append(
                f"[{e.entry_type}]事件{age}{pullback_ok}("
                f"Y={e.entry_price:.2f},Z={e.stop_loss:.2f},"
                f"有效至{e.expire_date})"
            )
        return "；".join(notes)
