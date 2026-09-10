# -*- coding: utf-8 -*-
import json
import sqlite3
from datetime import date

import pytest

from src.db import close_thread_connection, init_db
from src.analyzers.signal_lifecycle import DbSignalEventStore, SignalLifecycle
from src.push.templates import render_environment_overview
from src.rules_version import RULES_VERSION, get_rules_version


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    import src.db as db_module

    db_path = tmp_path / "rules-version.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    close_thread_connection()
    yield db_path
    close_thread_connection()


def test_report_header_stamps_global_rules_version():
    content = render_environment_overview({"market_mode": "defend"})
    assert get_rules_version() == RULES_VERSION
    assert f"规则版本:{RULES_VERSION}" in content


def test_new_event_snapshots_global_rules_version(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
    )

    saved = DbSignalEventStore().get_active_events("002975")[0]
    assert saved.rules_version == RULES_VERSION
    note = lifecycle.event_status_note("002975", current_price=101.0)
    assert f"规则:{RULES_VERSION}" in note


def test_fill_transition_logs_trigger_data_rule_and_version(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
    )
    lifecycle.evaluate_events(
        "002975",
        current_price=103.0,
        day_low=102.0,
        close_price=103.0,
        today=date(2026, 9, 10),
    )

    logs = DbSignalEventStore().get_event_logs(event.event_id)
    fill_log = next(log for log in logs if log["to_status"] == "filled")
    assert fill_log["rule_entry"] == "R3.2回踩确认"
    assert fill_log["rules_version"] == RULES_VERSION
    trigger = json.loads(fill_log["trigger_data"])
    assert trigger["day_low"] == 102.0
    assert trigger["entry_price"] == 102.77
    assert trigger["close"] == 103.0
