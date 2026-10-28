from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from ..models.journal import JournalEntry
from ..models.context_mark import ContextMark, ContextStatus, ContextType
from ..models.event_calendar import EventCalendar
from ..models.signal import Signal, SignalRole, SignalStatus, SignalValidPolicy

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
CREATE TABLE IF NOT EXISTS market_snapshots (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    est_close REAL NOT NULL,
    low REAL,
    high REAL,
    volume_ratio REAL,
    two_source_anomaly INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_date ON market_snapshots(trade_date);
CREATE TABLE IF NOT EXISTS zone_snapshots (
    signal_id TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (signal_id, effective_at)
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status);
CREATE INDEX IF NOT EXISTS idx_signals_parent ON signals(parent_id);

CREATE TABLE IF NOT EXISTS signal_trigger_snapshots (
    signal_id TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    side TEXT NOT NULL,
    tag TEXT NOT NULL,
    role TEXT NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    valid_policy TEXT NOT NULL,
    PRIMARY KEY (signal_id, triggered_at)
);

CREATE TABLE IF NOT EXISTS report_signal_snapshots (
    report_date TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    side TEXT NOT NULL,
    tag TEXT NOT NULL,
    role TEXT NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    size_hint INTEGER,
    premise TEXT NOT NULL DEFAULT '{}',
    break_qualifier TEXT,
    settlement_outcome TEXT,
    trend TEXT NOT NULL,
    activation TEXT,
    valid_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    trigger_window_start TEXT,
    trigger_window_end TEXT,
    PRIMARY KEY (report_date, signal_id)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    price REAL,
    record_type TEXT NOT NULL DEFAULT 'transition' CHECK (record_type IN ('transition','zone_update','qualifier')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_transition
ON journal(signal_id, created_at, from_status, to_status, reason, COALESCE(price, -1));
CREATE INDEX IF NOT EXISTS idx_journal_signal ON journal(signal_id, created_at);

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

CREATE TABLE IF NOT EXISTS correction_anchors (
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    qualifier TEXT NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    notice_price REAL,
    noticed_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (signal_id, trade_date)
);

CREATE TABLE IF NOT EXISTS correction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('same_day_close','retest_pass','post_break_retest')),
    notice TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(trade_date, signal_id, trigger)
);
CREATE INDEX IF NOT EXISTS idx_correction_events_date ON correction_events(trade_date);
"""


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _rebuild_correction_anchors_schema(conn)
        conn.execute("DROP TABLE IF EXISTS receipts")
        _rebuild_signals_schema(conn)
        conn.execute("UPDATE signals SET role='retest_confirm' WHERE tag='retest_confirm' AND role<>'retest_confirm'")
        _migrate_journal_schema(conn)
        conn.execute('''
            INSERT OR IGNORE INTO zone_snapshots (
                signal_id, effective_at, zone_low, zone_high, created_at
            )
            SELECT id, created_at, zone_low, zone_high, created_at FROM signals
        ''')
        conn.execute(
            "DELETE FROM signals WHERE tag IN ('index_context','sector_context','time_trigger')"
        )
        pool_columns = {row[1] for row in conn.execute('PRAGMA table_info(pool)')}
        if 'role' not in pool_columns:
            conn.execute("ALTER TABLE pool ADD COLUMN role TEXT NOT NULL DEFAULT 'holding'")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pool_symbol_theme_role ON pool(date, symbol, theme_label, role)")


def _rebuild_correction_anchors_schema(conn: sqlite3.Connection) -> None:
    columns = conn.execute('PRAGMA table_info(correction_anchors)').fetchall()
    if len([column for column in columns if column['pk'] == 1]) == 2:
        return
    conn.execute('ALTER TABLE correction_anchors RENAME TO correction_anchors_legacy')
    conn.execute('''
        CREATE TABLE correction_anchors (
            signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            qualifier TEXT NOT NULL,
            zone_low REAL NOT NULL,
            zone_high REAL NOT NULL,
            notice_price REAL,
            noticed_at TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (signal_id, trade_date)
        )
    ''')
    conn.execute('''
        INSERT INTO correction_anchors (
            signal_id, symbol, trade_date, qualifier,
            zone_low, zone_high, notice_price, noticed_at, created_at
        )
        SELECT
            signal_id, symbol, trade_date, qualifier,
            zone_low, zone_high, notice_price, noticed_at, created_at
        FROM correction_anchors_legacy
    ''')
    conn.execute('DROP TABLE correction_anchors_legacy')


def _rebuild_signals_schema(conn: sqlite3.Connection) -> None:
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()[0]
    required = ('role', 'break_qualifier', 'trigger_window_start', 'trigger_window_end')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(signals)')}
    if 'settlement_outcome' not in columns:
        conn.execute('ALTER TABLE signals ADD COLUMN settlement_outcome TEXT')
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
        FROM signals
        WHERE tag NOT IN ('index_context','sector_context','time_trigger');
    ''')
    conn.execute('DROP TABLE signals')
    conn.execute('ALTER TABLE signals_v8 RENAME TO signals')
    conn.execute('CREATE INDEX idx_signals_symbol_status ON signals(symbol, status)')
    conn.execute('CREATE INDEX idx_signals_parent ON signals(parent_id)')

    signal_columns = {row[1] for row in conn.execute('PRAGMA table_info(signals)')}
    if 'settlement_outcome' not in signal_columns:
        conn.execute('ALTER TABLE signals ADD COLUMN settlement_outcome TEXT')


def _migrate_journal_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(journal)')}
    if 'record_type' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN record_type TEXT NOT NULL DEFAULT 'transition'")
    if 'confirmed' in columns:
        conn.execute('DROP INDEX IF EXISTS uq_journal_transition')
        conn.execute('ALTER TABLE journal RENAME TO journal_legacy')
        conn.execute('''
            CREATE TABLE journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                price REAL,
                record_type TEXT NOT NULL DEFAULT 'transition' CHECK (record_type IN ('transition','zone_update','qualifier')),
                created_at TEXT NOT NULL
            )
        ''')
        conn.execute('''
            INSERT INTO journal (
                id, signal_id, from_status, to_status, reason, price, record_type, created_at
            )
            SELECT id, signal_id, from_status, to_status, reason, price, record_type, created_at
            FROM journal_legacy
        ''')
        conn.execute('DROP TABLE journal_legacy')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_transition ON journal(signal_id, created_at, from_status, to_status, reason, COALESCE(price, -1))')


def save_signal(conn: sqlite3.Connection, signal: Signal) -> None:
    conn.execute(
        '''INSERT OR REPLACE INTO signals (
               id, symbol, strategy_id, side, tag, role, zone_low, zone_high, size_hint,
               premise, break_qualifier, settlement_outcome, trend, activation,
               valid_policy, status, status_reason, parent_id, created_at, closed_at,
               trigger_window_start, trigger_window_end
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id, signal.symbol, signal.strategy_id, signal.side.value,
            signal.tag.value, signal.signal_role.value, signal.zone_low, signal.zone_high, signal.size_hint,
            json.dumps(signal.premise, ensure_ascii=False), signal.break_qualifier, signal.settlement_outcome,
            signal.trend.value, signal.activation,
            signal.valid_policy.value, signal.status.value, signal.status_reason,
            signal.parent_id, signal.created_at.isoformat(timespec='seconds'),
            signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
            signal.trigger_window_start.isoformat(timespec='seconds') if signal.trigger_window_start else None,
            signal.trigger_window_end.isoformat(timespec='seconds') if signal.trigger_window_end else None,
        ),
    )
    save_zone_snapshot(
        conn,
        signal.id,
        signal.zone_low,
        signal.zone_high,
        signal.created_at,
    )


def save_trigger_snapshot(conn: sqlite3.Connection, signal: Signal, triggered_at: datetime) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO signal_trigger_snapshots (
               signal_id, triggered_at, symbol, strategy_id, side, tag, role,
               zone_low, zone_high, parent_id, created_at, valid_policy
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id,
            triggered_at.isoformat(timespec='seconds'),
            signal.symbol,
            signal.strategy_id,
            signal.side.value,
            signal.tag.value,
            signal.signal_role.value,
            signal.zone_low,
            signal.zone_high,
            signal.parent_id,
            signal.created_at.isoformat(timespec='seconds'),
            signal.valid_policy.value,
        ),
    )


def save_signals(conn: sqlite3.Connection, signals: list[Signal]) -> int:
    for signal in signals:
        save_signal(conn, signal)
    return len(signals)


def save_zone_snapshot(
    conn: sqlite3.Connection,
    signal_id: str,
    zone_low: float,
    zone_high: float,
    effective_at: datetime,
) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO zone_snapshots (
               signal_id, effective_at, zone_low, zone_high, created_at
           ) VALUES (?,?,?,?,?)''',
        (
            signal_id,
            effective_at.isoformat(timespec='seconds'),
            float(zone_low),
            float(zone_high),
            datetime.now().astimezone().isoformat(timespec='seconds'),
        ),
    )


def load_zone_snapshot(
    conn: sqlite3.Connection,
    signal_id: str,
    effective_at: str,
) -> tuple[float, float] | None:
    row = conn.execute(
        '''
        SELECT zone_low, zone_high FROM zone_snapshots
        WHERE signal_id=? AND effective_at<=?
        ORDER BY effective_at DESC LIMIT 1
        ''',
        (signal_id, effective_at),
    ).fetchone()
    if row is None:
        return None
    return float(row['zone_low']), float(row['zone_high'])


def save_market_snapshots(
    conn: sqlite3.Connection,
    trade_date: str,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        conn.execute(
            '''INSERT OR REPLACE INTO market_snapshots (
                   trade_date, symbol, est_close, low, high, volume_ratio,
                   two_source_anomaly, created_at
               ) VALUES (?,?,?,?,?,?,?,?)''',
            (
                trade_date,
                str(row['symbol']),
                float(row['est_close']),
                float(row['low']) if row.get('low') is not None else None,
                float(row['high']) if row.get('high') is not None else None,
                float(row['volume_ratio']) if row.get('volume_ratio') is not None else None,
                1 if row.get('two_source_anomaly') else 0,
                datetime.now().astimezone().isoformat(timespec='seconds'),
            ),
        )
    return len(rows)


def load_market_snapshots(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> list[dict[str, Any]]:
    if trade_date:
        rows = conn.execute(
            'SELECT * FROM market_snapshots WHERE trade_date=? ORDER BY symbol',
            (trade_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM market_snapshots ORDER BY trade_date, symbol'
        ).fetchall()
    return [
        {
            'trade_date': str(row['trade_date']),
            'symbol': str(row['symbol']),
            'est_close': row['est_close'],
            'low': row['low'],
            'high': row['high'],
            'volume_ratio': row['volume_ratio'],
            'two_source_anomaly': bool(row['two_source_anomaly']),
        }
        for row in rows
    ]


def save_report_signal_snapshot(
    conn: sqlite3.Connection,
    report_date: str,
    signals: list[Signal],
) -> int:
    for signal in signals:
        conn.execute(
            '''INSERT OR REPLACE INTO report_signal_snapshots (
                   report_date, signal_id, symbol, strategy_id, side, tag, role,
                   zone_low, zone_high, size_hint, premise, break_qualifier,
                   settlement_outcome, trend, activation, valid_policy, status,
                   status_reason, parent_id, created_at, closed_at,
                   trigger_window_start, trigger_window_end
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                report_date,
                signal.id,
                signal.symbol,
                signal.strategy_id,
                signal.side.value,
                signal.tag.value,
                signal.signal_role.value,
                signal.zone_low,
                signal.zone_high,
                signal.size_hint,
                json.dumps(signal.premise, ensure_ascii=False),
                signal.break_qualifier,
                signal.settlement_outcome,
                signal.trend.value,
                signal.activation,
                signal.valid_policy.value,
                signal.status.value,
                signal.status_reason,
                signal.parent_id,
                signal.created_at.isoformat(timespec='seconds'),
                signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
                signal.trigger_window_start.isoformat(timespec='seconds') if signal.trigger_window_start else None,
                signal.trigger_window_end.isoformat(timespec='seconds') if signal.trigger_window_end else None,
            ),
        )
    return len(signals)


def load_report_signal_snapshot(
    conn: sqlite3.Connection,
    report_date: str,
) -> list[Signal]:
    rows = conn.execute(
        'SELECT * FROM report_signal_snapshots WHERE report_date=? ORDER BY created_at, signal_id',
        (report_date,),
    ).fetchall()
    signals = []
    for row in rows:
        item = dict(row)
        item['id'] = item.pop('signal_id')
        item.pop('report_date')
        signals.append(signal_from_row(item))
    return signals


def save_journal(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    if entry.to_status == 'TRIGGERED':
        signal_row = conn.execute(
            'SELECT * FROM signals WHERE id=?', (entry.signal_id,)
        ).fetchone()
        if signal_row is not None:
            save_trigger_snapshot(conn, signal_from_row(signal_row), entry.created_at)
    conn.execute(
        '''INSERT OR IGNORE INTO journal (
               signal_id, from_status, to_status, reason, price,
               record_type, created_at
           ) VALUES (?,?,?,?,?,?,?)''',
        (
            entry.signal_id, entry.from_status, entry.to_status, entry.reason,
            entry.price, entry.record_type.value,
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


def save_correction_anchor(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    symbol: str,
    trade_date: str,
    qualifier: str,
    zone_low: float,
    zone_high: float,
    created_at: datetime,
) -> bool:
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO correction_anchors (
               signal_id, symbol, trade_date, qualifier,
               zone_low, zone_high, created_at
           ) VALUES (?,?,?,?,?,?,?)''',
        (
            signal_id, symbol, trade_date, qualifier,
            float(zone_low), float(zone_high),
            created_at.isoformat(timespec='seconds'),
        ),
    )
    return cursor.rowcount > 0


def save_correction_event(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    signal_id: str,
    symbol: str,
    trigger: str,
    notice: str,
    created_at: datetime,
) -> bool:
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO correction_events (
               trade_date, signal_id, symbol, trigger, notice, created_at
           ) VALUES (?,?,?,?,?,?)''',
        (
            trade_date, signal_id, symbol, trigger, notice,
            created_at.isoformat(timespec='seconds'),
        ),
    )
    return cursor.rowcount > 0


def load_correction_events(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''
            SELECT ce.trade_date, ce.signal_id, ce.symbol, ce.trigger,
                   ce.notice, ce.created_at, s.strategy_id
            FROM correction_events
            ce
            JOIN signals s ON s.id = ce.signal_id
            WHERE ce.trade_date=?
            ORDER BY ce.created_at, ce.id
            ''',
            (trade_date,),
        ).fetchall()
    ]


def signal_from_row(row: sqlite3.Row | dict[str, Any]) -> Signal:
    item = dict(row)
    if isinstance(item.get('premise'), str):
        try:
            item['premise'] = json.loads(item['premise'])
        except json.JSONDecodeError:
            item['premise'] = {}
    if 'settlement_outcome' not in item:
        item['settlement_outcome'] = None
    return Signal.model_validate(item)


def load_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    sql = 'SELECT * FROM signals'
    params: tuple[str, ...] = ()
    if trade_date:
        sql += ' WHERE created_at LIKE ?'
        params = (trade_date + '%',)
    sql += ' ORDER BY created_at, id'
    return [signal_from_row(row) for row in conn.execute(sql, params).fetchall()]


def load_active_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    return [signal for signal in load_signals(conn, trade_date) if signal.status is SignalStatus.ACTIVE]


def load_pending_correction_anchors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''
            SELECT ca.signal_id, ca.symbol, ca.trade_date, ca.qualifier,
                   ca.zone_low, ca.zone_high, s.strategy_id
            FROM correction_anchors ca
            JOIN signals s ON s.id = ca.signal_id
            WHERE ca.noticed_at IS NULL
            ORDER BY ca.trade_date, ca.signal_id
            '''
        ).fetchall()
    ]


def mark_correction_notice(
    conn: sqlite3.Connection,
    signal_id: str,
    price: float,
    noticed_at: datetime,
) -> bool:
    cursor = conn.execute(
        '''
        UPDATE correction_anchors
        SET notice_price=?, noticed_at=?
        WHERE signal_id=? AND noticed_at IS NULL
        ''',
        (
            float(price), noticed_at.isoformat(timespec='seconds'), signal_id,
        ),
    )
    return cursor.rowcount > 0


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
