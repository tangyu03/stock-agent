from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, Optional

from ..models.journal import JournalEntry
from ..models.signal import Signal

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
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    tag TEXT NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    size_hint INTEGER,
    premise TEXT NOT NULL DEFAULT '{}',
    trend TEXT NOT NULL CHECK (trend IN ('UP','DOWN')),
    activation TEXT,
    valid_policy TEXT NOT NULL CHECK (valid_policy IN ('intraday','until_stopped')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','TRIGGERED','EXPIRED','INVALIDATED')),
    status_reason TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
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
"""


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(pool)')}
        if 'role' not in pool_columns:
            conn.execute("ALTER TABLE pool ADD COLUMN role TEXT NOT NULL DEFAULT 'holding'")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pool_symbol_theme_role ON pool(date, symbol, theme_label, role)")


def save_signal(conn: sqlite3.Connection, signal: Signal) -> None:
    conn.execute(
        '''INSERT OR REPLACE INTO signals (
               id, symbol, strategy_id, side, tag, zone_low, zone_high, size_hint,
               premise, trend, activation, valid_policy, status, status_reason,
               parent_id, created_at, closed_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id, signal.symbol, signal.strategy_id, signal.side.value,
            signal.tag.value, signal.zone_low, signal.zone_high, signal.size_hint,
            json.dumps(signal.premise, ensure_ascii=False), signal.trend.value, signal.activation,
            signal.valid_policy.value, signal.status.value, signal.status_reason,
            signal.parent_id, signal.created_at.isoformat(timespec='seconds'),
            signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
        ),
    )


def save_signals(conn: sqlite3.Connection, signals: list[Signal]) -> int:
    for signal in signals:
        save_signal(conn, signal)
    return len(signals)


def save_journal(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO journal (
               signal_id, from_status, to_status, reason, price, confirmed, created_at
           ) VALUES (?,?,?,?,?,?,?)''',
        (
            entry.signal_id, entry.from_status, entry.to_status, entry.reason,
            entry.price, entry.confirmed.value, entry.created_at.isoformat(timespec='seconds'),
        ),
    )


def session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.commit()
