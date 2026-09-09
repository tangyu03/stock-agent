# -*- coding: utf-8 -*-
import sqlite3

import pytest

from src.db import close_thread_connection, init_db, _migrate
from src.analyzers.signal_lifecycle import (
    DbSignalEventStore,
    InMemorySignalEventStore,
    SignalLifecycle,
)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    import src.db as db_module

    db_path = tmp_path / "signal-events.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    close_thread_connection()
    yield db_path
    close_thread_connection()


def test_register_event_persists_y_formula_and_inputs(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "300666", "沃尔德", "价量突破",
        breakout_level=99.39, entry_price=102.07, stop_loss=91.44,
        target_low=112.89, target_high=120.21,
        hypothesis={"y": 102.07},
        y_formula="breakout_day_low",
        y_inputs={"breakout_day_low": 102.07},
    )

    saved = DbSignalEventStore().get_active_events("300666")[0]
    assert saved.event_id == event.event_id
    assert saved.y_formula == "breakout_day_low"
    assert saved.y_inputs == '{"breakout_day_low": 102.07}'


def test_old_signal_events_table_is_migrated(sqlite_db):
    conn = sqlite3.connect(sqlite_db)
    conn.execute("""
        CREATE TABLE signal_events (
            event_id TEXT PRIMARY KEY,
            stock_code TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            born_date TEXT NOT NULL,
            status TEXT DEFAULT 'valid'
        )
    """)
    conn.commit()
    conn.close()

    _migrate()
    conn = sqlite3.connect(sqlite_db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_events)")}
    conn.close()

    assert {"rule_version", "y_formula", "y_inputs"} <= columns


def test_event_transition_logs_are_queryable_by_event_id(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=91.44,
        target_low=112.89, target_high=120.21,
        hypothesis={"y": 102.77},
    )
    lifecycle.invalidate(event.event_id, "回踩失败已撤单")

    logs = DbSignalEventStore().get_event_logs(event.event_id)
    assert [(log["from_status"], log["to_status"]) for log in logs] == [
        ("none", "valid"), ("valid", "invalidated"),
    ]
    assert logs[-1]["reason"] == "回踩失败已撤单"


def test_in_memory_store_keeps_transition_log():
    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=91.44,
        target_low=112.89, target_high=120.21,
        hypothesis={"y": 102.77},
    )
    lifecycle.mark_triggered(event.event_id)

    store = lifecycle.store
    assert [(log["from_status"], log["to_status"]) for log in store.logs] == [
        ("none", "valid"), ("valid", "filled"),
    ]
