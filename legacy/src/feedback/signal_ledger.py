"""Signal ledger projection over the single event-library source."""

import json
import re
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
    CREATE TABLE IF NOT EXISTS signal_tracking_stop_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        log_date TEXT NOT NULL,
        event_stop REAL,
        tracking_stop REAL,
        current_price REAL,
        rules_version TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(event_id, log_date)
    )
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_signal_tracking_stop_logs_event
    ON signal_tracking_stop_logs(event_id, log_date)
    """)
    cursor.execute("DROP VIEW IF EXISTS signal_ledger")
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
                    COALESCE(
                        json_extract(
                            (SELECT trigger_data FROM signal_event_logs
                             WHERE event_id = e.event_id
                             ORDER BY id DESC LIMIT 1), '$.today'
                        ),
                        (SELECT created_at FROM signal_event_logs
                         WHERE event_id = e.event_id ORDER BY id DESC LIMIT 1)
                    ),
                    1, 10
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


def record_tracking_stop(
    event_id: str,
    log_date: str,
    event_stop: Optional[float],
    tracking_stop: Optional[float],
    current_price: Optional[float],
    rules_version: str = "",
) -> bool:
    """Append the daily tracking-stop snapshot; intraday replays are ignored."""
    if not event_id or not log_date:
        return False
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            ensure_signal_ledger_schema(cursor)
            cursor.execute(
                """
                INSERT OR IGNORE INTO signal_tracking_stop_logs
                (event_id, log_date, event_stop, tracking_stop,
                 current_price, rules_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    log_date,
                    float(event_stop) if event_stop else None,
                    float(tracking_stop) if tracking_stop else None,
                    float(current_price) if current_price else None,
                    rules_version,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception:
        return False


def get_tracking_stop_logs(event_id: str = "", log_date: str = "") -> List[dict]:
    clauses = []
    params: List[object] = []
    if event_id:
        clauses.append("event_id = ?")
        params.append(event_id)
    if log_date:
        clauses.append("log_date = ?")
        params.append(log_date)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cursor = conn.cursor()
        ensure_signal_ledger_schema(cursor)
        cursor.execute(
            f"SELECT * FROM signal_tracking_stop_logs{where} ORDER BY log_date, event_id",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


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


def _snapshot_status(status: object) -> str:
    value = str(status or "")
    return "filled" if value == "triggered" else value


def backfill_snapshots_from_transition_logs() -> dict:
    """Backfill close snapshots only from event-log-backed prices.

    This is an append-only reconciliation pass.  Missing prices remain missing;
    unknown historical events are never fabricated.
    """
    from ..analyzers.signal_lifecycle import _ensure_tables

    filled = 0
    skipped = 0
    missing: List[dict] = []
    with get_conn() as conn:
        cursor = conn.cursor()
        _ensure_tables(cursor)
        ensure_signal_ledger_schema(cursor)
        events = [dict(row) for row in cursor.execute(
            "SELECT * FROM signal_events ORDER BY born_date"
        )]
        for event in events:
            logs = [dict(row) for row in cursor.execute(
                "SELECT * FROM signal_event_logs WHERE event_id=? ORDER BY id",
                (event["event_id"],),
            )]
            existing = {
                row["snapshot_date"]: dict(row)
                for row in cursor.execute(
                    "SELECT snapshot_date, status, source FROM "
                    "signal_event_daily_snapshots WHERE event_id=?",
                    (event["event_id"],),
                )
            }
            candidates: dict[str, dict] = {}
            for log in logs:
                try:
                    raw = log.get("trigger_data") or "{}"
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    data = {}
                snapshot_date = str(data.get("today") or log.get("created_at") or "")[:10]
                if not snapshot_date:
                    continue
                slot = candidates.setdefault(snapshot_date, {
                    "price": 0.0,
                    "status": _snapshot_status(log.get("to_status") or event["status"]),
                    "source": "transition_log",
                })
                if log.get("to_status"):
                    slot["status"] = _snapshot_status(log.get("to_status"))
                for key in ("close", "price"):
                    try:
                        price = float(data.get(key) or 0)
                    except (TypeError, ValueError):
                        continue
                    if price > 0:
                        slot["price"] = price
                        slot["source"] = "transition_log"
            reason = str(event.get("invalid_reason") or "")
            match = re.search(r"收盘(?:价)?([0-9]+(?:\.[0-9]+)?)", reason)
            if match:
                date_text = ""
                terminal_status = ""
                for log in logs:
                    date_text = str(log.get("created_at") or "")[:10]
                    terminal_status = _snapshot_status(log.get("to_status"))
                    if date_text:
                        break
                if date_text and date_text not in candidates:
                    candidates[date_text] = {
                        "price": float(match.group(1)),
                        "status": terminal_status
                        or _snapshot_status(event["status"]),
                        "source": "terminal_reason",
                    }
            if not candidates and not existing:
                missing.append({
                    "signal_id": event["event_id"],
                    "stock_name": event["stock_name"],
                    "reason": "迁移日志无价格",
                })
                continue
            for snapshot_date, item in candidates.items():
                if snapshot_date in existing:
                    skipped += 1
                    continue
                price = float(item.get("price") or 0)
                if price <= 0:
                    skipped += 1
                    continue
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO signal_event_daily_snapshots
                    (event_id, snapshot_date, close_price, status, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"], snapshot_date, price,
                        str(item.get("status") or _snapshot_status(event["status"])),
                        str(item.get("source") or "transition_log"),
                    ),
                )
                if cursor.rowcount:
                    filled += 1
                else:
                    skipped += 1
        conn.commit()
    return {"filled": filled, "candidate_count": skipped, "missing": missing}
