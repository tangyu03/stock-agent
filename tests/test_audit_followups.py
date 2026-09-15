from datetime import date
from pathlib import Path
import uuid

import pytest

from src.db import close_thread_connection, init_db


@pytest.fixture
def sqlite_db(monkeypatch):
    import src.db as db_module

    db_dir = Path(".ab_tmp") / f"audit-followups-{uuid.uuid4().hex}"
    db_dir.mkdir(parents=True, exist_ok=False)
    db_path = db_dir / "audit-followups.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    close_thread_connection()
    yield db_path
    close_thread_connection()
    import shutil

    shutil.rmtree(db_dir, ignore_errors=True)


def test_mode_matrix_and_panic_use_chinese_disclosure():
    from src.decision.mode_rules import (
        build_mode_transition_matrix,
        evaluate_market_panic,
        render_market_panic_line,
    )

    panic = evaluate_market_panic(
        index_drop=-4.5, gem_star_drop=-5.5, ad_ratio=0.13,
    )
    assert panic["triggered"] is True
    text = render_market_panic_line(panic)
    assert "已触发" in text
    assert "triggered" not in text

    matrix = build_mode_transition_matrix("defend")
    assert [row["mode"] for row in matrix] == ["attack", "defend", "retreat"]
    assert next(row for row in matrix if row["mode"] == "defend")["is_current"]


def test_panic_blocker_shares_market_panic_rule():
    from src.orchestrator.unified_engine import _panic_bottom_blocker

    tech = {
        "index_daily_drop": -4.5,
        "gem_sci_tech_drop": -5.5,
        "advance_decline_ratio": 0.13,
    }
    assert _panic_bottom_blocker(tech) == "缺个股超卖"

    tech["change_pct"] = -8.0
    assert _panic_bottom_blocker(tech) == "已见恐慌/超卖，但确认不足"

    tech.update({
        "index_daily_drop": 0.5,
        "gem_sci_tech_drop": 0.5,
        "advance_decline_ratio": 1.2,
    })
    assert _panic_bottom_blocker(tech) == "正常行情，未触发"


def test_sector_changes_snapshot_and_render(sqlite_db):
    from src.feedback.sector_changes import record_sector_changes
    from src.push.templates import render_environment_overview

    baseline = record_sector_changes(
        "2026-09-10",
        {"算力": "main_trend"},
        {"300308": "算力"},
        {"300308": "main_trend"},
        name_by_code={"300308": "中际旭创"},
    )
    assert baseline["baseline"] is True

    changed = record_sector_changes(
        "2026-09-11",
        {"算力": "rotational"},
        {"300308": "半导体"},
        {"300308": "rotational"},
        name_by_code={"300308": "中际旭创"},
    )
    assert changed["status_changes"] == [{
        "sector": "算力",
        "from": "main_trend",
        "to": "rotational",
        "from_label": "主线",
        "to_label": "轮动",
    }]
    assert changed["mapping_changes"][0]["to_sector"] == "半导体"

    text = render_environment_overview({"sector_changes": changed})
    assert "板块变更" in text
    assert "算力: 主线→轮动" in text
    assert "中际旭创(300308): 主线→轮动" in text


def test_tracking_stop_is_persisted_once_per_day(sqlite_db):
    from src.feedback.signal_ledger import (
        get_tracking_stop_logs,
        record_tracking_stop,
    )

    event_id = "evt-tracking"
    assert record_tracking_stop(
        event_id, "2026-09-13", 85.54, 90.30, 97.00, "v2.5",
    )
    assert not record_tracking_stop(
        event_id, "2026-09-13", 85.54, 90.30, 96.00, "v2.5",
    )
    assert record_tracking_stop(
        event_id, "2026-09-14", 85.54, 91.00, 94.00, "v2.5",
    )

    rows = get_tracking_stop_logs(event_id)
    assert [(row["log_date"], row["tracking_stop"]) for row in rows] == [
        ("2026-09-13", 90.30), ("2026-09-14", 91.00),
    ]


def test_backfill_uses_transition_status_and_is_idempotent(sqlite_db):
    from src.analyzers.signal_lifecycle import DbSignalEventStore, SignalLifecycle
    from src.db import get_conn
    from src.feedback.signal_ledger import backfill_snapshots_from_transition_logs

    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
        born=date(2026, 9, 7),
    )

    logs = [
        (
            "valid", "filled", "价格触及买点，转入成交跟踪",
            '{"today":"2026-09-09","close":103.00}',
            "R3.2回踩确认",
        ),
        (
            "filled", "time_exit", "到期未触目标/止损，信号口径平",
            '{"today":"2026-09-14","close":95.29}',
            "R3.9时间离场",
        ),
    ]
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO signal_event_logs
            (event_id, stock_code, from_status, to_status, reason, source,
             trigger_data, rule_entry)
            VALUES (?, ?, ?, ?, ?, 'test_backfill', ?, ?)
            """,
            [
                (event.event_id, event.stock_code, from_status, to_status,
                 reason, trigger_data, rule_entry)
                for from_status, to_status, reason, trigger_data, rule_entry
                in logs
            ],
        )
        conn.commit()

    result = backfill_snapshots_from_transition_logs()
    assert result["filled"] == 2
    assert result["missing"] == []
    with get_conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT snapshot_date, close_price, status FROM "
            "signal_event_daily_snapshots WHERE event_id=? ORDER BY snapshot_date",
            (event.event_id,),
        )]
    assert rows == [
        {"snapshot_date": "2026-09-09", "close_price": 103.0, "status": "filled"},
        {"snapshot_date": "2026-09-14", "close_price": 95.29, "status": "time_exit"},
    ]

    second = backfill_snapshots_from_transition_logs()
    assert second["filled"] == 0
    assert second["missing"] == []


def test_backfill_does_not_duplicate_existing_snapshot(sqlite_db):
    from src.analyzers.signal_lifecycle import DbSignalEventStore, SignalLifecycle
    from src.db import get_conn
    from src.feedback.signal_ledger import (
        backfill_snapshots_from_transition_logs,
        record_daily_snapshot,
    )

    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "300308", "中际旭创", "价量突破",
        breakout_level=891.17, entry_price=907.00, stop_loss=817.99,
        target_low=950.0, target_high=980.0,
        born=date(2026, 9, 10),
    )
    assert record_daily_snapshot(
        event.event_id, "2026-09-11", 101.0, "valid",
    )
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO signal_event_logs
            (event_id, stock_code, from_status, to_status, reason, source,
             trigger_data, rule_entry)
            VALUES (?, ?, 'valid', 'filled', '回踩确认', 'test_backfill',
                    '{"today":"2026-09-11","close":102.0}', 'R3.2回踩确认')
            """,
            (event.event_id, event.stock_code),
        )
        conn.commit()

    result = backfill_snapshots_from_transition_logs()
    assert result["filled"] == 0
    assert not any(item["signal_id"] == event.event_id for item in result["missing"])
    with get_conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT snapshot_date, close_price, status, source FROM "
            "signal_event_daily_snapshots WHERE event_id=?",
            (event.event_id,),
        )]
    assert rows == [{
        "snapshot_date": "2026-09-11",
        "close_price": 101.0,
        "status": "valid",
        "source": "signal_lifecycle",
    }]


def test_bojie_filled_event_exits_by_time():
    from src.analyzers.signal_lifecycle import (
        InMemorySignalEventStore,
        SignalLifecycle,
    )

    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
        born=date(2026, 9, 7),
    )
    lifecycle.evaluate_events(
        "002975", current_price=102.77, day_low=102.77,
        day_high=104.0, close_price=103.0, today=date(2026, 9, 8),
    )
    notices = lifecycle.evaluate_events(
        "002975", current_price=95.29, day_low=95.0,
        day_high=96.0, close_price=95.29, today=date(2026, 9, 14),
    )

    assert lifecycle.store.events[event.event_id].status == "time_exit"
    assert lifecycle.get_active_events("002975") == []
    assert any(item["exit_type"] == "时间离场" for item in notices)
    assert lifecycle.store.logs[-1]["rule_entry"] == "R3.9时间离场"
