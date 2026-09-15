from datetime import date
from pathlib import Path
import uuid

import pytest

from src.analyzers.signal_lifecycle import DbSignalEventStore, SignalLifecycle
from src.db import close_thread_connection, init_db
from src.feedback.signal_ledger import get_signal_ledger


@pytest.fixture
def sqlite_db(monkeypatch):
    import src.db as db_module

    db_dir = Path(".ab_tmp") / f"signal-ledger-{uuid.uuid4().hex}"
    db_dir.mkdir(parents=True, exist_ok=False)
    db_path = db_dir / "signal-ledger.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    close_thread_connection()
    yield db_path
    close_thread_connection()
    import shutil

    shutil.rmtree(db_dir, ignore_errors=True)


def test_ledger_projects_event_lifecycle_and_close_snapshot(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
    )
    lifecycle.evaluate_events(
        "002975", current_price=103.0, day_low=102.0,
        close_price=103.0, today=date(2026, 9, 10),
    )
    # 同日重放不应产生第二条收盘快照。
    lifecycle.evaluate_events(
        "002975", current_price=103.0, day_low=102.0,
        close_price=103.0, today=date(2026, 9, 10),
    )

    rows = get_signal_ledger(event.event_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["stock_code"] == "002975"
    assert row["strategy"] == "价量突破"
    assert row["buy_price"] == 102.77
    assert row["status_transition_chain"] == "none->valid | valid->filled"
    assert row["daily_close_snapshot"] == "2026-09-10:103.00"
    assert row["current_status"] == "filled"
    assert row["completion_status"] == "filled"
    assert row["completion_date"] == "2026-09-10"
    assert row["completion_price"] == 103.0


def test_ledger_uses_single_event_source_and_supports_code_lookup(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "688028", "沃尔德", "价量突破",
        breakout_level=88.0, entry_price=89.50, stop_loss=83.50,
        target_low=96.0, target_high=102.0,
    )
    lifecycle.evaluate_events(
        "688028", current_price=86.0, close_price=86.0,
        today=date(2026, 9, 10),
    )

    rows = get_signal_ledger(stock_code="688028")
    assert len(rows) == 1
    assert rows[0]["signal_id"] == event.event_id
    assert "valid->invalidated" in rows[0]["status_transition_chain"]
    assert rows[0]["completion_status"] == "invalidated"
    assert rows[0]["completion_price"] == 86.0
