import sqlite3
import tempfile
from pathlib import Path

import pytest

from pointtable.db.migrate import migrate
from pointtable.db.session import get_connection


@pytest.fixture()
def source_db(request) -> Path:
    tmp_path = Path.cwd()
    path = tmp_path / "legacy.db"
    path = tmp_path / request.node.name
    path = path.with_suffix(".db")
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE signal_events (
            event_id TEXT PRIMARY KEY, stock_code TEXT, entry_type TEXT, born_date TEXT,
            expire_date TEXT, breakout_level REAL, entry_price REAL, stop_loss REAL,
            target_low REAL, target_high REAL, status TEXT, invalid_reason TEXT,
            entry_snapshot TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT
        );
        CREATE TABLE signal_event_logs (
            event_id TEXT, from_status TEXT, to_status TEXT, reason TEXT,
            trigger_data TEXT DEFAULT '{}', created_at TEXT
        );
        CREATE TABLE trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, stock_code TEXT,
            user_action TEXT, actual_position REAL, created_at TEXT, event_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO signal_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("evt-1", "300001", "volume_breakout", "2026-01-01", "2026-01-02",
         9.9, 10.0, 9.0, 11.0, 12.0, "valid", "", "{}", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
    )
    conn.execute(
        "INSERT INTO signal_event_logs VALUES (?,?,?,?,?,?)",
        ("evt-1", "ACTIVE", "TRIGGERED", "hit", '{"price":10.1}', "2026-01-01 10:00:00"),
    )
    conn.execute(
        "INSERT INTO trade_logs (date,stock_code,user_action,actual_position,created_at,event_id)"
        " VALUES (?,?,?,?,?,?)",
        ("2026-01-01", "300001", "executed", 300, "2026-01-01 10:01:00", "evt-1"),
    )
    conn.commit()
    conn.close()
    return path


def test_migration_is_idempotent(source_db: Path):
    tmp_path = source_db.parent
    output_dir = source_db.with_suffix(".output")
    database_path = output_dir / "pointtable.db"
    database_path.unlink(missing_ok=True)
    first = migrate(source_db, output_dir, database_path)
    assert first["checks"]["signals_inserted"] == 1
    assert first["checks"]["journal_inserted"] == 1
    assert first["receipts"]["inserted"] == 1

    conn = get_connection()
    assert conn.execute("select count(*) from signals").fetchone()[0] == 1
    assert conn.execute("select count(*) from journal").fetchone()[0] == 1
    assert conn.execute("select count(*) from receipts").fetchone()[0] == 1
    conn.commit()

    second = migrate(source_db, output_dir, database_path)
    assert second["checks"]["signals_inserted"] == 0
    assert second["checks"]["journal_inserted"] == 0
    assert second["receipts"]["inserted"] == 0


def test_migration_exports_history_csv(source_db: Path):
    tmp_path = source_db.parent
    output_dir = tmp_path / "history"
    migrate(source_db, output_dir, tmp_path / "pointtable.db")
    assert (output_dir / "signal_events.csv").exists()
    assert (output_dir / "signal_event_logs.csv").exists()
    assert (output_dir / "reconciliation.json").exists()
