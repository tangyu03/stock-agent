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
    valid_for TEXT,
    pair_id TEXT,
    root_id TEXT,
    created_trade_date TEXT,
    window_days INTEGER,
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
CREATE TABLE IF NOT EXISTS market_days (
    trade_date TEXT PRIMARY KEY,
    market_tier TEXT NOT NULL,
    index_trend TEXT NOT NULL,
    circuit_day INTEGER NOT NULL CHECK (circuit_day IN (0,1)),
    evaluated_at TEXT NOT NULL,
    rule_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stop_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    raw_qualifier TEXT,
    effective_qualifier TEXT,
    circuit_day INTEGER NOT NULL CHECK (circuit_day IN (0,1)),
    close_price REAL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    zone_snapshot_id TEXT,
    settlement_outcome TEXT NOT NULL,
    data_status TEXT NOT NULL DEFAULT 'OK',
    correction_scene TEXT,
    event_source TEXT NOT NULL CHECK (event_source IN ('live_settle','replay','backfill')),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    code_version TEXT,
    input_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(signal_id, trade_date, event_source)
);
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
    valid_for TEXT,
    pair_id TEXT,
    root_id TEXT,
    created_trade_date TEXT,
    window_days INTEGER,
    event_source TEXT NOT NULL DEFAULT 'live_settle',
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
    valid_for TEXT,
    pair_id TEXT,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    trigger_window_start TEXT,
    trigger_window_end TEXT,
    root_id TEXT,
    created_trade_date TEXT,
    window_days INTEGER,
    PRIMARY KEY (report_date, signal_id)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    price REAL,
    event TEXT,
    qualifier TEXT,
    old_zone_low REAL,
    old_zone_high REAL,
    new_zone_low REAL,
    new_zone_high REAL,
    record_type TEXT NOT NULL DEFAULT 'transition' CHECK (record_type IN ('transition','zone_update','qualifier')),
    event_source TEXT NOT NULL DEFAULT 'live_settle',
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
    data_status TEXT NOT NULL DEFAULT 'OK',
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
    scene TEXT,
    source_signal_id TEXT,
    notice_status TEXT CHECK (notice_status IN ('pending','pushed','suppressed','failed')),
    dedup_key TEXT,
    event_source TEXT NOT NULL DEFAULT 'live_settle',
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


def _ensure_signal_columns(conn: sqlite3.Connection) -> None:
    """signals 表幂等补列：覆盖两种存量旧库。

    - v8 形状但缺后期新增列的库（有 role 等列时 _rebuild_signals_schema 会早退，
      走不到重建，列必须在此补齐）；
    - 走 signals_v8 重建路径生成的表（CREATE TABLE 模板未含这些列）。
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info(signals)')}
    for name, decl in (
        ('settlement_outcome', 'TEXT'),
        ('valid_for', 'TEXT'),
        ('pair_id', 'TEXT'),
        ('root_id', 'TEXT'),
        ('created_trade_date', 'TEXT'),
        ('window_days', 'INTEGER'),
    ):
        if name not in columns:
            conn.execute(f'ALTER TABLE signals ADD COLUMN {name} {decl}')


def _rebuild_signals_schema(conn: sqlite3.Connection) -> None:
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()[0]
    required = ('role', 'break_qualifier', 'trigger_window_start', 'trigger_window_end')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(signals)')}
    _ensure_signal_columns(conn)
    for table in ('signal_trigger_snapshots', 'report_signal_snapshots'):
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if 'valid_for' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN valid_for TEXT')
        if 'pair_id' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN pair_id TEXT')
        if 'root_id' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN root_id TEXT')
        if 'created_trade_date' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN created_trade_date TEXT')
        if 'window_days' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN window_days INTEGER')
        if 'event_source' not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN event_source TEXT NOT NULL DEFAULT 'live_settle'")
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

    _ensure_signal_columns(conn)


def _migrate_journal_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(journal)')}
    if 'record_type' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN record_type TEXT NOT NULL DEFAULT 'transition'")
    if 'event_source' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN event_source TEXT NOT NULL DEFAULT 'live_settle'")
    if 'occurred_at' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN occurred_at TEXT")
    if 'recorded_at' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN recorded_at TEXT")
    if 'zone_snapshot_id' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN zone_snapshot_id TEXT")
    if 'run_id' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN run_id TEXT")
    if 'code_version' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN code_version TEXT")
    if 'input_snapshot_id' not in columns:
        conn.execute("ALTER TABLE journal ADD COLUMN input_snapshot_id TEXT")
    if 'event' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN event TEXT')
    if 'qualifier' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN qualifier TEXT')
    if 'old_zone_low' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN old_zone_low REAL')
    if 'old_zone_high' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN old_zone_high REAL')
    if 'new_zone_low' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN new_zone_low REAL')
    if 'new_zone_high' not in columns:
        conn.execute('ALTER TABLE journal ADD COLUMN new_zone_high REAL')
    context_columns = {
        row[1] for row in conn.execute('PRAGMA table_info(context_marks)')
    }
    if 'data_status' not in context_columns:
        conn.execute("ALTER TABLE context_marks ADD COLUMN data_status TEXT NOT NULL DEFAULT 'OK'")
    correction_columns = {
        row[1] for row in conn.execute('PRAGMA table_info(correction_events)')
    }
    if 'scene' not in correction_columns:
        conn.execute('ALTER TABLE correction_events ADD COLUMN scene TEXT')
    if 'source_signal_id' not in correction_columns:
        conn.execute('ALTER TABLE correction_events ADD COLUMN source_signal_id TEXT')
    if 'notice_status' not in correction_columns:
        conn.execute('ALTER TABLE correction_events ADD COLUMN notice_status TEXT')
    if 'dedup_key' not in correction_columns:
        conn.execute('ALTER TABLE correction_events ADD COLUMN dedup_key TEXT')
    if 'event_source' not in correction_columns:
        conn.execute("ALTER TABLE correction_events ADD COLUMN event_source TEXT NOT NULL DEFAULT 'live_settle'")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_correction_events_dedup ON correction_events(dedup_key)')
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
                event TEXT,
                qualifier TEXT,
                old_zone_low REAL,
                old_zone_high REAL,
                new_zone_low REAL,
                new_zone_high REAL,
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
               valid_policy, valid_for, pair_id, root_id, created_trade_date, window_days,
               status, status_reason, parent_id, created_at, closed_at,
               trigger_window_start, trigger_window_end
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id, signal.symbol, signal.strategy_id, signal.side.value,
            signal.tag.value, signal.signal_role.value, signal.zone_low, signal.zone_high, signal.size_hint,
            json.dumps(signal.premise, ensure_ascii=False), signal.break_qualifier, signal.settlement_outcome,
            signal.trend.value, signal.activation,
            signal.valid_policy.value,
            signal.valid_for.isoformat() if signal.valid_for else None,
            signal.pair_id,
            signal.root_id,
            signal.created_trade_date.isoformat() if signal.created_trade_date else None,
            signal.window_days,
            signal.status.value, signal.status_reason,
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


def save_trigger_snapshot(
    conn: sqlite3.Connection,
    signal: Signal,
    triggered_at: datetime,
    event_source: str = 'live_settle',
) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO signal_trigger_snapshots (
               signal_id, triggered_at, symbol, strategy_id, side, tag, role,
               zone_low, zone_high, parent_id, root_id, created_trade_date, window_days, created_at, valid_policy,
               valid_for, pair_id, event_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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
            signal.root_id,
            signal.created_trade_date.isoformat() if signal.created_trade_date else None,
            signal.window_days,
            signal.created_at.isoformat(timespec='seconds'),
            signal.valid_policy.value,
            signal.valid_for.isoformat() if signal.valid_for else None,
            signal.pair_id,
            event_source,
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


def save_market_day(
    conn: sqlite3.Connection,
    trade_date: str,
    market_tier: str,
    index_trend: str,
    *,
    evaluated_at: datetime,
    rule_version: str = 'circuit-v1',
    volume_tier: str = 'LOW',
    index_trend_rule: str = 'DOWN',
) -> None:
    from ..core.volume import is_circuit_day

    conn.execute(
        '''INSERT INTO market_days (
               trade_date, market_tier, index_trend, circuit_day,
               evaluated_at, rule_version
           ) VALUES (?,?,?,?,?,?)
           ON CONFLICT(trade_date) DO UPDATE SET
               market_tier=excluded.market_tier,
               index_trend=excluded.index_trend,
               circuit_day=excluded.circuit_day,
               evaluated_at=excluded.evaluated_at,
               rule_version=excluded.rule_version''',
        (
            trade_date,
            market_tier,
            index_trend,
            1 if is_circuit_day(
                market_tier,
                index_trend,
                volume_tier=volume_tier,
                index_trend_rule=index_trend_rule,
            ) else 0,
            evaluated_at.isoformat(timespec='seconds'),
            rule_version,
        ),
    )


def load_market_day(conn: sqlite3.Connection, trade_date: str) -> dict[str, Any] | None:
    row = conn.execute('SELECT * FROM market_days WHERE trade_date=?', (trade_date,)).fetchone()
    if row is None:
        return None
    return {
        'trade_date': str(row['trade_date']),
        'market_tier': str(row['market_tier']),
        'index_trend': str(row['index_trend']),
        'circuit_day': bool(row['circuit_day']),
        'evaluated_at': str(row['evaluated_at']),
        'rule_version': str(row['rule_version']),
    }


def save_stop_settlement(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    trade_date: str,
    raw_qualifier: str | None,
    effective_qualifier: str,
    circuit_day: bool,
    close_price: float | None,
    zone_low: float,
    zone_high: float,
    settlement_outcome: str,
    occurred_at: datetime,
    correction_scene: str | None = None,
    event_source: str = 'live_settle',
    data_status: str = 'OK',
    code_version: str = 'local',
    input_snapshot_id: str | None = None,
) -> bool:
    recorded_at = datetime.now().astimezone().isoformat(timespec='seconds')
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO stop_settlements (
               signal_id, trade_date, raw_qualifier, effective_qualifier, circuit_day,
               close_price, zone_low, zone_high, zone_snapshot_id, settlement_outcome,
               data_status, correction_scene, event_source, occurred_at, recorded_at,
               code_version, input_snapshot_id, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal_id, trade_date, raw_qualifier, effective_qualifier,
            1 if circuit_day else 0, close_price, zone_low, zone_high,
            signal_id + ':' + occurred_at.isoformat(timespec='seconds'),
            settlement_outcome, data_status, correction_scene, event_source,
            occurred_at.isoformat(timespec='seconds'), recorded_at,
            code_version, input_snapshot_id,
            recorded_at,
        ),
    )
    return cursor.rowcount > 0


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
                   settlement_outcome, trend, activation, valid_policy,
                   valid_for, pair_id, root_id, created_trade_date, window_days,
                   status, status_reason, parent_id, created_at, closed_at,
                   trigger_window_start, trigger_window_end
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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
                signal.valid_for.isoformat() if signal.valid_for else None,
                signal.pair_id,
                signal.root_id,
                signal.created_trade_date.isoformat() if signal.created_trade_date else None,
                signal.window_days,
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
        item.pop('event_source', None)
        signals.append(signal_from_row(item))
    return signals


def save_journal(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    if entry.to_status == 'TRIGGERED':
        signal_row = conn.execute(
            'SELECT * FROM signals WHERE id=?', (entry.signal_id,)
        ).fetchone()
        if signal_row is not None:
            save_trigger_snapshot(
                conn,
                signal_from_row(signal_row),
                entry.created_at,
                event_source=entry.event_source,
            )
    conn.execute(
        '''INSERT OR IGNORE INTO journal (
               signal_id, from_status, to_status, reason, price,
               event, qualifier, old_zone_low, old_zone_high,
               new_zone_low, new_zone_high,
               record_type, event_source, created_at, occurred_at, recorded_at,
               zone_snapshot_id, run_id, code_version, input_snapshot_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            entry.signal_id, entry.from_status, entry.to_status, entry.reason,
            entry.price,
            entry.event, entry.qualifier,
            entry.old_zone[0] if entry.old_zone else None,
            entry.old_zone[1] if entry.old_zone else None,
            entry.new_zone[0] if entry.new_zone else None,
            entry.new_zone[1] if entry.new_zone else None,
            entry.record_type.value, entry.event_source,
            entry.created_at.isoformat(timespec='seconds'),
            (entry.occurred_at or entry.created_at).isoformat(timespec='seconds'),
            (entry.recorded_at or datetime.now().astimezone()).isoformat(timespec='seconds'),
            entry.zone_snapshot_id, entry.run_id, entry.code_version,
            entry.input_snapshot_id,
        ),
    )


def save_context_marks(conn: sqlite3.Connection, marks: list[ContextMark]) -> int:
    for mark in marks:
        conn.execute(
            '''INSERT OR REPLACE INTO context_marks (
                   date, context_type, code, level_low, level_high, label,
                   status, data_status, triggered_at, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                mark.date.isoformat(), mark.context_type.value, mark.code,
                mark.level_low, mark.level_high, mark.label, mark.status.value,
                mark.data_status,
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
    scene: str | None = None,
    source_signal_id: str | None = None,
    notice_status: str = 'pending',
    dedup_key: str | None = None,
    event_source: str = 'live_settle',
) -> bool:
    effective_scene = scene or ('same_day_recover' if trigger == 'same_day_close' else trigger)
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO correction_events (
               trade_date, signal_id, symbol, trigger, notice, created_at,
               scene, source_signal_id, notice_status, dedup_key, event_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (
            trade_date, signal_id, symbol, trigger, notice,
            created_at.isoformat(timespec='seconds'), effective_scene,
            source_signal_id or signal_id, notice_status, dedup_key,
            event_source,
        ),
    )
    return cursor.rowcount > 0


def load_correction_events(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''
            SELECT ce.trade_date, ce.signal_id, ce.symbol, ce.trigger,
                   ce.notice, ce.created_at, ce.scene, ce.source_signal_id,
                   ce.notice_status, ce.dedup_key, ce.event_source, s.strategy_id
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


def load_stop_settlements(
    conn: sqlite3.Connection,
    trade_date: str,
    event_source: str | None = None,
) -> list[dict[str, Any]]:
    sql = 'SELECT * FROM stop_settlements WHERE trade_date=?'
    params: list[Any] = [trade_date]
    if event_source:
        sql += ' AND event_source=?'
        params.append(event_source)
    sql += ' ORDER BY signal_id, event_source'
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


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
        data_status=str(row['data_status'] or 'OK') if 'data_status' in row.keys() else 'OK',
        triggered_at=datetime.fromisoformat(row['triggered_at']) if row['triggered_at'] else None,
        created_at=datetime.fromisoformat(row['created_at']),
    )


def session() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.commit()
