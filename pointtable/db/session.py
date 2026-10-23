from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterator, Optional

from ..models.journal import JournalEntry
from ..models.context_mark import ContextMark, ContextStatus, ContextType
from ..models.event_calendar import EventCalendar
from ..models.signal import Signal, SignalRole, SignalValidPolicy

_DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "pointtable.db"
_local = threading.local()


def set_database_path(path: str | Path) -> None:
    global _DATABASE_PATH
    existing = getattr(_local, "conn", None)
    if existing is not None:
        existing.close()
        _local.conn = None
    _DATABASE_PATH = Path(path).resolve()


def database_path() -> Path:
    return _DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        _DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL','NEUTRAL')),
    tag TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('entry','stop','profit','t','retest_confirm')),
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    size_hint INTEGER,
    premise TEXT NOT NULL DEFAULT '{}',
    break_qualifier TEXT,
    trend TEXT NOT NULL CHECK (trend IN ('UP','DOWN')),
    activation TEXT,
    valid_policy TEXT NOT NULL CHECK (valid_policy IN ('intraday','next_day','same_day','fixed_window','until_stopped')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','TRIGGERED','EXPIRED','INVALIDATED')),
    status_reason TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    trigger_window_start TEXT,
    trigger_window_end TEXT,
    CHECK (zone_low < zone_high)
);

CREATE TABLE IF NOT EXISTS tail_snapshots (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'spot',
    PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status);
CREATE INDEX IF NOT EXISTS idx_signals_parent ON signals(parent_id);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    price REAL,
    confirmed TEXT NOT NULL CHECK (confirmed IN ('confirmed','touched_only')),
    record_type TEXT NOT NULL DEFAULT 'transition' CHECK (record_type IN ('transition','zone_update','qualifier')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_transition
ON journal(signal_id, created_at, from_status, to_status, reason, COALESCE(price, -1), confirmed);
CREATE INDEX IF NOT EXISTS idx_journal_signal ON journal(signal_id, created_at);

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('executed','ignored','veto')),
    size_filled INTEGER,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_transition
ON receipts(signal_id, verdict, COALESCE(size_filled, -1), created_at);
CREATE INDEX IF NOT EXISTS idx_receipts_signal ON receipts(signal_id, created_at);

CREATE TABLE IF NOT EXISTS pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    theme_label TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'holding' CHECK (role IN ('holding','index','sector','candidate')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, symbol, theme_label, role)
);

CREATE TABLE IF NOT EXISTS context_marks (
    date TEXT NOT NULL,
    context_type TEXT NOT NULL CHECK (context_type IN ('index','sector')),
    code TEXT NOT NULL,
    level_low REAL NOT NULL,
    level_high REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE')),
    triggered_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, context_type, code)
);

CREATE TABLE IF NOT EXISTS event_calendar (
    date TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'time_trigger',
    code TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    due_time TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, event_type, code, due_time)
);
"""


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _rebuild_signals_schema(conn)
        _migrate_journal_schema(conn)
        conn.execute(
            "DELETE FROM signals WHERE tag IN ('index_context','sector_context','time_trigger')"
        )
        pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(pool)')}
        if 'role' not in pool_columns:
            conn.execute("ALTER TABLE pool ADD COLUMN role TEXT NOT NULL DEFAULT 'holding'")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pool_symbol_theme_role ON pool(date, symbol, theme_label, role)")


def _rebuild_signals_schema(conn: sqlite3.Connection) -> None:
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()[0]
    required = ('role', 'break_qualifier', 'trigger_window_start', 'trigger_window_end')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(signals)')}
    if all(column in columns for column in required) and all(value in table_sql for value in required):
        return
    conn.execute('DROP TABLE IF EXISTS signals_v8')
    conn.executescript('''
        CREATE TABLE signals_v8 (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('BUY','SELL','NEUTRAL')),
            tag TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('entry','stop','profit','t','retest_confirm')),
            zone_low REAL NOT NULL,
            zone_high REAL NOT NULL,
            size_hint INTEGER,
            premise TEXT NOT NULL DEFAULT '{}',
            break_qualifier TEXT,
            trend TEXT NOT NULL CHECK (trend IN ('UP','DOWN')),
            activation TEXT,
            valid_policy TEXT NOT NULL CHECK (valid_policy IN ('intraday','next_day','same_day','fixed_window','until_stopped')),
            status TEXT NOT NULL CHECK (status IN ('ACTIVE','TRIGGERED','EXPIRED','INVALIDATED')),
            status_reason TEXT NOT NULL DEFAULT '',
            parent_id TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            trigger_window_start TEXT,
            trigger_window_end TEXT,
            CHECK (zone_low < zone_high)
        );
        INSERT INTO signals_v8 (
            id, symbol, strategy_id, side, tag, role, zone_low, zone_high, size_hint,
            premise, break_qualifier, trend, activation, valid_policy, status,
            status_reason, parent_id, created_at, closed_at, trigger_window_start, trigger_window_end
        )
        SELECT
            id, symbol, strategy_id,
            CASE tag WHEN 'retest_confirm' THEN 'NEUTRAL' ELSE side END,
            tag,
            CASE tag
                WHEN 'enter' THEN 'entry'
                WHEN 'retest_confirm' THEN 'retest_confirm'
                ELSE tag
            END,
            zone_low, zone_high, size_hint,
            premise, NULL, trend, activation, valid_policy, status,
            status_reason, parent_id, created_at, closed_at, NULL, NULL
        FROM signals;
    ''')
    conn.execute('DROP TABLE signals')
    conn.execute('ALTER TABLE signals_v8 RENAME TO signals')
    conn.execute('CREATE INDEX idx_signals_symbol_status ON signals(symbol, status)')
    conn.execute('CREATE INDEX idx_signals_parent ON signals(parent_id)')


def _migrate_journal_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(journal)')}
    if 'record_type' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN record_type TEXT NOT NULL DEFAULT 'transition'")


def save_signal(conn: sqlite3.Connection, signal: Signal) -> None:
    conn.execute(
        '''INSERT OR REPLACE INTO signals (
               id, symbol, strategy_id, side, tag, role, zone_low, zone_high, size_hint,
               premise, break_qualifier, trend, activation, valid_policy, status,
               status_reason, parent_id, created_at, closed_at,
               trigger_window_start, trigger_window_end
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id, signal.symbol, signal.strategy_id, signal.side.value,
            signal.tag.value, signal.signal_role.value, signal.zone_low, signal.zone_high, signal.size_hint,
            json.dumps(signal.premise, ensure_ascii=False), signal.break_qualifier, signal.trend.value, signal.activation,
            signal.valid_policy.value, signal.status.value, signal.status_reason,
            signal.parent_id, signal.created_at.isoformat(timespec='seconds'),
            signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
            signal.trigger_window_start.isoformat(timespec='seconds') if signal.trigger_window_start else None,
            signal.trigger_window_end.isoformat(timespec='seconds') if signal.trigger_window_end else None,
        ),
    )


def save_signals(conn: sqlite3.Connection, signals: list[Signal]) -> int:
    for signal in signals:
        save_signal(conn, signal)
    return len(signals)


def save_journal(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO journal (
               signal_id, from_status, to_status, reason, price, confirmed,
               record_type, created_at
           ) VALUES (?,?,?,?,?,?,?,?)''',
        (
            entry.signal_id, entry.from_status, entry.to_status, entry.reason,
            entry.price, entry.confirmed.value, entry.record_type.value,
            entry.created_at.isoformat(timespec='seconds'),
        ),
    )


def save_context_marks(conn: sqlite3.Connection, marks: list[ContextMark]) -> int:
    for mark in marks:
        conn.execute(
            '''INSERT OR REPLACE INTO context_marks (
                   date, context_type, code, level_low, level_high, label,
                   status, triggered_at, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                mark.date.isoformat(), mark.context_type.value, mark.code,
                mark.level_low, mark.level_high, mark.label, mark.status.value,
                mark.triggered_at.isoformat(timespec='seconds') if mark.triggered_at else None,
                mark.created_at.isoformat(timespec='seconds'),
            ),
        )
    return len(marks)


def save_event_calendar(conn: sqlite3.Connection, events: list[EventCalendar]) -> int:
    for event in events:
        conn.execute(
            '''INSERT OR REPLACE INTO event_calendar (
                   date, event_type, code, label, due_time, payload, created_at
               ) VALUES (?,?,?,?,?,?,?)''',
            (
                event.date.isoformat(), event.event_type, event.code, event.label,
                event.due_time.isoformat(timespec='seconds'),
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at.isoformat(timespec='seconds'),
            ),
        )
    return len(events)


def load_event_calendar(
    conn: sqlite3.Connection,
    trade_date: str,
    event_type: str | None = 'time_trigger',
) -> list[EventCalendar]:
    sql = 'SELECT * FROM event_calendar WHERE date=?'
    params: list[object] = [trade_date]
    if event_type is not None:
        sql += ' AND event_type=?'
        params.append(event_type)
    sql += ' ORDER BY due_time, code'
    rows = []
    for row in conn.execute(sql, params).fetchall():
        rows.append(EventCalendar(
            date=date.fromisoformat(row['date']),
            event_type=str(row['event_type']),
            code=str(row['code']),
            label=str(row['label']),
            due_time=time.fromisoformat(row['due_time']),
            payload=json.loads(row['payload']),
            created_at=datetime.fromisoformat(row['created_at']),
        ))
    return rows


def load_context_marks(
    conn: sqlite3.Connection,
    trade_date: str,
    status: ContextStatus | None = ContextStatus.ACTIVE,
) -> list[ContextMark]:
    sql = 'SELECT * FROM context_marks WHERE date=?'
    params: list[object] = [trade_date]
    if status is not None:
        sql += ' AND status=?'
        params.append(status.value)
    return [_context_mark_from_row(row) for row in conn.execute(sql, params).fetchall()]


def touch_context_mark(
    conn: sqlite3.Connection,
    trade_date: str,
    context_type: ContextType,
    code: str,
    triggered_at: datetime,
) -> bool:
    cursor = conn.execute(
        'UPDATE context_marks SET triggered_at=?, status=? WHERE date=? AND context_type=? AND code=?',
        (
            triggered_at.isoformat(timespec='seconds'), ContextStatus.ACTIVE.value,
            trade_date, context_type.value, code,
        ),
    )
    return cursor.rowcount > 0


def _context_mark_from_row(row: sqlite3.Row) -> ContextMark:
    return ContextMark(
        date=date.fromisoformat(row['date']),
        context_type=ContextType(row['context_type']),
        code=row['code'],
        level_low=row['level_low'],
        level_high=row['level_high'],
        label=row['label'],
        status=ContextStatus(row['status']),
        triggered_at=datetime.fromisoformat(row['triggered_at']) if row['triggered_at'] else None,
        created_at=datetime.fromisoformat(row['created_at']),
    )


def session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.commit()
