from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .session import init_db, get_connection, set_database_path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DB = REPO_ROOT / "data" / "stock_agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "pointtable" / "data" / "migration"

_STATUS_MAP = {
    "valid": "ACTIVE",
    "frozen": "ACTIVE",
    "triggered": "TRIGGERED",
    "filled": "TRIGGERED",
    "expired": "EXPIRED",
    "time_exit": "EXPIRED",
    "sig_target": "EXPIRED",
    "invalidated": "INVALIDATED",
    "chase_abandon": "INVALIDATED",
}


def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).replace("T", " ")
    return text if len(text) >= 19 else f"{text} 00:00:00"


def _status(old_status: str) -> str:
    return _STATUS_MAP.get(str(old_status or "").lower(), "ACTIVE")


def _closed_at(old_status: str, updated_at: Any) -> Optional[str]:
    return _iso(updated_at) if _status(old_status) in {"EXPIRED", "INVALIDATED"} else None


def _export_table(source: sqlite3.Connection, table: str, output_path: Path) -> int:
    cursor = source.execute(f"SELECT * FROM {table}")
    columns = [item[0] for item in cursor.description or []]
    rows = cursor.fetchall()
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(zip(columns, row)))
    return len(rows)


def _migrate_signals(source: sqlite3.Connection, conn: sqlite3.Connection) -> dict[str, int]:
    rows = source.execute(
        "SELECT event_id, stock_code, entry_type, born_date, expire_date, entry_price,"
        " target_low, target_high, stop_loss, status, invalid_reason, entry_snapshot, created_at, updated_at"
        " FROM signal_events ORDER BY created_at, event_id"
    ).fetchall()
    inserted = skipped = 0
    for row in rows:
        entry = float(row["entry_price"] or row["breakout_level"] or 0)
        target = float(row["target_low"] or 0)
        zone_low = min(entry, target)
        zone_high = max(entry, target)
        if zone_low <= 0 or zone_high <= zone_low:
            skipped += 1
            continue
        cursor = conn.execute(
            """INSERT OR IGNORE INTO signals (
                id, symbol, strategy_id, side, tag, zone_low, zone_high, size_hint,
                premise, trend, activation, valid_policy, status, status_reason,
                parent_id, created_at, closed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(row["event_id"]),
                str(row["stock_code"]),
                str(row["entry_type"] or "unknown"),
                "BUY",
                "enter",
                zone_low,
                zone_high,
                None,
                json.dumps(
                    {
                        "source": "signal_events",
                        "entry_snapshot": json.loads(row["entry_snapshot"] or "{}"),
                        "stop_loss": row["stop_loss"],
                        "target_high": row["target_high"],
                    },
                    ensure_ascii=False,
                ),
                "UP",
                None,
                "until_stopped" if row["expire_date"] else "intraday",
                _status(row["status"]),
                str(row["invalid_reason"] or f"old_status:{row['status']}"),
                None,
                _iso(row["created_at"] or row["born_date"]),
                _closed_at(row["status"], row["updated_at"]),
            ),
        )
        inserted += cursor.rowcount
    return {"source": len(rows), "inserted": inserted, "skipped": skipped}


def _migrate_journal(source: sqlite3.Connection, conn: sqlite3.Connection) -> dict[str, int]:
    rows = source.execute(
        "SELECT event_id, from_status, to_status, reason, trigger_data, created_at"
        " FROM signal_event_logs ORDER BY created_at, rowid"
    ).fetchall()
    inserted = skipped = 0
    for row in rows:
        try:
            trigger = json.loads(row["trigger_data"] or "{}")
        except json.JSONDecodeError:
            trigger = {}
        price = trigger.get("price") or trigger.get("current_price")
        to_status = str(row["to_status"] or "").lower()
        confirmed = (
            "confirmed" if to_status in {"triggered", "filled", "sig_target", "time_exit"} else "touched_only"
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO journal (
                signal_id, from_status, to_status, reason, price, confirmed, created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                str(row["event_id"]),
                str(row["from_status"] or "none"),
                str(row["to_status"] or "unknown"),
                str(row["reason"] or ""),
                float(price) if price not in (None, "") else None,
                confirmed,
                _iso(row["created_at"]) or "1970-01-01 00:00:00",
            ),
        )
        inserted += cursor.rowcount
        if cursor.rowcount == 0:
            skipped += 1
    return {"source": len(rows), "inserted": inserted, "skipped": skipped}


def _migrate_receipts(source: sqlite3.Connection, conn: sqlite3.Connection) -> dict[str, int]:
    rows = source.execute(
        "SELECT id, user_action, actual_position, created_at, event_id FROM trade_logs"
        " WHERE user_action IN ('executed','ignored','veto') ORDER BY created_at, id"
    ).fetchall()
    inserted = skipped = 0
    for row in rows:
        signal_id = str(row["event_id"] or "").strip()
        if not signal_id:
            skipped += 1
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO receipts (signal_id, verdict, size_filled, created_at)"
            " VALUES (?,?,?,?)",
            (
                signal_id,
                str(row["user_action"]),
                int(row["actual_position"]) if row["actual_position"] else None,
                _iso(row["created_at"]) or "1970-01-01 00:00:00",
            ),
        )
        inserted += cursor.rowcount
        if cursor.rowcount == 0:
            skipped += 1
    return {"source": len(rows), "inserted": inserted, "skipped": skipped}


def _load_theme_map() -> dict[str, str]:
    path = REPO_ROOT / "pointtable" / "config" / "theme_map.yaml"
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            str(code).strip(): str(label).strip()
            for code, label in (data.get("overrides") or {}).items()
        }
    except Exception:
        return {}


def _load_portfolio_stocks() -> list[dict[str, str]]:
    path = REPO_ROOT / "config" / "portfolio.yaml"
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [
            {"symbol": str(item.get("code")).strip(), "theme_label": "unknown"}
            for item in (data.get("stocks") or []) if item.get("code")
        ]
    except Exception:
        return []


def _migrate_pool(conn: sqlite3.Connection, source: sqlite3.Connection) -> dict[str, int]:
    rows = [
        (str(row["date"] or ""), str(row["stock_code"] or ""))
        for row in source.execute(
            "SELECT DISTINCT date, stock_code FROM trade_logs ORDER BY date DESC, stock_code"
        )
    ]
    theme_map = _load_theme_map()
    inserted = 0
    today = date.today().isoformat()
    portfolio_rows = [(today, item["symbol"]) for item in _load_portfolio_stocks()]
    rows = portfolio_rows + rows
    for row in rows:
        symbol = str(row[1] or "").strip()
        if not symbol:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO pool (date, symbol, theme_label) VALUES (?,?,?)",
            (str(row[0] or today), symbol, theme_map.get(symbol, "unknown")),
        )
        inserted += cursor.rowcount
    return {"source": len(rows), "inserted": inserted, "skipped": 0}


def migrate(
    source_db: Path = DEFAULT_SOURCE_DB,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    database_path: Path | None = None,
) -> dict[str, Any]:
    source_db = source_db.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if database_path is not None:
        set_database_path(database_path)
    init_db()
    source = sqlite3.connect(source_db)
    source.row_factory = sqlite3.Row
    source.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    conn = get_connection()
    exports = {
        "trade_logs": _export_table(source, "trade_logs", output_dir / "trade_logs.csv"),
        "signal_events": _export_table(source, "signal_events", output_dir / "signal_events.csv"),
        "signal_event_logs": _export_table(source, "signal_event_logs", output_dir / "signal_event_logs.csv"),
    }
    signals = _migrate_signals(source, conn)
    journal = _migrate_journal(source, conn)
    receipts = _migrate_receipts(source, conn)
    pool = _migrate_pool(conn, source)
    totals = {
        "signals": conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
        "journal": conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0],
        "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
        "pool": conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0],
    }
    conn.commit()
    source.close()
    reconciliation = {
        "source_db": str(source_db),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "exports": exports,
        "signals": signals,
        "journal": journal,
        "receipts": receipts,
        "pool": pool,
        "totals": totals,
        "checks": {
            "signal_events_source": exports["signal_events"],
            "signals_inserted": signals["inserted"],
            "journal_source": exports["signal_event_logs"],
            "journal_inserted": journal["inserted"],
        },
    }
    (output_dir / "reconciliation.json").write_text(
        json.dumps(reconciliation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return reconciliation


def export_assets(output_dir: Path | None = None) -> dict[str, str]:
    target = (output_dir or REPO_ROOT / "pointtable" / "config").resolve()
    target.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for name in ("theme_map.yaml", "push.yaml"):
        source = REPO_ROOT / "pointtable" / "config" / name
        if source.exists():
            destination = target / name
            if source != destination:
                shutil.copy2(source, destination)
                copied[name] = str(destination)
    portfolio = REPO_ROOT / "config" / "portfolio.yaml"
    if portfolio.exists():
        shutil.copy2(portfolio, target / "portfolio_source.yaml")
        copied["portfolio_source.yaml"] = str(target / "portfolio_source.yaml")
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate stock-agent2 tables into v6 four-table schema")
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--assets-only", action="store_true")
    args = parser.parse_args()
    copied = export_assets()
    if not args.assets_only:
        result = migrate(args.source_db, args.output_dir, args.db)
        result["assets"] = copied
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(copied, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
