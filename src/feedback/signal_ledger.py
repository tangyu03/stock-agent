"""Signal ledger projection over the single event-library source."""

import json
from typing import List, Optional

from ..db import get_conn


def ensure_signal_ledger_schema(cursor=None) -> None:
    """Create the append-only daily snapshot table and read-only ledger view."""
    if cursor is None:
        with get_conn() as conn:
            ensure_signal_ledger_schema(conn.cursor())
            conn.commit()
        return

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_event_daily_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        close_price REAL,
        status TEXT NOT NULL,
        source TEXT DEFAULT 'signal_lifecycle',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(event_id, snapshot_date, source)
    )
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_signal_event_daily_snapshots_event
    ON signal_event_daily_snapshots(event_id, snapshot_date)
    """)
    cursor.execute("""
    CREATE VIEW IF NOT EXISTS signal_ledger AS
    SELECT
        e.event_id AS signal_id,
        e.stock_code,
        e.stock_name,
        e.entry_type AS strategy,
        e.rules_version,
        e.born_date AS t0_date,
        e.entry_price AS buy_price,
        e.stop_loss,
        e.target_low,
        e.target_high,
        e.expire_date,
        e.status AS current_status,
        (
            SELECT group_concat(chain, ' | ')
            FROM (
                SELECT from_status || '->' || to_status AS chain
                FROM signal_event_logs
                WHERE event_id = e.event_id
                ORDER BY id
            )
        ) AS status_transition_chain,
        (
            SELECT group_concat(snapshot, ' | ')
            FROM (
                SELECT snapshot_date || ':' || printf('%.2f', close_price) AS snapshot
                FROM signal_event_daily_snapshots
                WHERE event_id = e.event_id
                ORDER BY snapshot_date
            )
        ) AS daily_close_snapshot,
        (
            SELECT group_concat(line, ' | ')
            FROM (
                SELECT created_at || ' ' || from_status || '->' || to_status
                       || ' ' || reason AS line
                FROM signal_event_logs
                WHERE event_id = e.event_id
                  AND (from_status = 'frozen' OR to_status = 'frozen')
                ORDER BY id
            )
        ) AS arbitration_record,
        e.invalid_reason,
        CASE
            WHEN e.status IN ('filled', 'invalidated', 'expired', 'chase_abandon',
                              'sig_target', 'sig_stop', 'time_exit') THEN
                substr(
                    (SELECT created_at FROM signal_event_logs
                     WHERE event_id = e.event_id ORDER BY id DESC LIMIT 1), 1, 10
                )
            ELSE ''
        END AS completion_date,
        e.status AS completion_status,
        CASE
            WHEN e.status IN ('filled', 'invalidated', 'expired', 'chase_abandon',
                              'sig_target', 'sig_stop', 'time_exit') THEN
                COALESCE(
                    json_extract((SELECT trigger_data FROM signal_event_logs
                                  WHERE event_id = e.event_id
                                  ORDER BY id DESC LIMIT 1), '$.close'),
                    json_extract((SELECT trigger_data FROM signal_event_logs
                                  WHERE event_id = e.event_id
                                  ORDER BY id DESC LIMIT 1), '$.price'),
                    json_extract((SELECT trigger_data FROM signal_event_logs
                                  WHERE event_id = e.event_id
                                  ORDER BY id DESC LIMIT 1), '$.day_low')
                )
            ELSE NULL
        END AS completion_price
    FROM signal_events e
    """)


def record_daily_snapshot(
    event_id: str,
    snapshot_date: str,
    close_price: Optional[float],
    status: str,
) -> bool:
    """Append one close snapshot; replays for the same date are ignored."""
    if not event_id or not snapshot_date:
        return False
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            ensure_signal_ledger_schema(cursor)
            cursor.execute(
                """
                INSERT OR IGNORE INTO signal_event_daily_snapshots
                (event_id, snapshot_date, close_price, status)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, snapshot_date, close_price, status),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception:
        return False


def get_signal_ledger(
    signal_id: str = "",
    stock_code: str = "",
) -> List[dict]:
    """Read the auditable projection; never mutate the event library."""
    clauses = []
    params = []
    if signal_id:
        clauses.append("signal_id = ?")
        params.append(signal_id)
    if stock_code:
        clauses.append("stock_code = ?")
        params.append(stock_code)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cursor = conn.cursor()
        ensure_signal_ledger_schema(cursor)
        cursor.execute(f"SELECT * FROM signal_ledger{where} ORDER BY t0_date, signal_id", params)
        return [dict(row) for row in cursor.fetchall()]
