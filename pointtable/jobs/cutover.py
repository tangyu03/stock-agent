from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

import yaml


@dataclass(frozen=True)
class CutoverResult:
    expired_count: int
    journal_count: int
    reason: str
    push_channel: str = 'formal'


def expire_legacy_signals(
    conn: sqlite3.Connection,
    now: datetime,
    reason: str = 'system_migration',
) -> CutoverResult:
    now_text = now.isoformat(timespec='seconds')
    rows = conn.execute(
        "SELECT id, status FROM signals WHERE status='ACTIVE' OR status='TRIGGERED' ORDER BY id"
    ).fetchall()
    journal_count = 0
    for row in rows:
        update_sql = "UPDATE signals SET status='EXPIRED', status_reason=?, closed_at=? WHERE id=? AND status=?"
        conn.execute(update_sql, (reason, now_text, row['id'], row['status']))
        insert_sql = "INSERT OR IGNORE INTO journal (signal_id, from_status, to_status, reason, confirmed, created_at) VALUES (?,?,?,?,?,?)"
        before_changes = conn.total_changes
        conn.execute(insert_sql, (row['id'], row['status'], 'EXPIRED', reason, 'confirmed', now_text))
        journal_count += conn.total_changes - before_changes
    return CutoverResult(len(rows), journal_count, reason)


def formal_push_ready(config_path: str | Path | None = None) -> bool:
    path = config_path or Path(__file__).parents[1] / 'config' / 'push.yaml'
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    return bool((raw.get('pushplus') or {}).get('token'))


def push_mode(config_path: str | Path | None = None) -> str:
    path = config_path or Path(__file__).parents[1] / 'config' / 'push.yaml'
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    return str((raw.get('pushplus') or {}).get('mode') or 'formal')


def switch_push_mode(config_path: str | Path | None = None, mode: str = 'formal') -> str:
    if mode not in {'shadow', 'formal'}:
        raise ValueError('push mode must be shadow or formal')
    path = config_path or Path(__file__).parents[1] / 'config' / 'push.yaml'
    text = path.read_text(encoding='utf-8')
    if re.search(r'^  mode:', text, flags=re.MULTILINE):
        text = re.sub(r'^  mode:.*$', f'  mode: "{mode}"', text, flags=re.MULTILINE)
    else:
        text = text.replace('pushplus:\n', f'pushplus:\n  mode: "{mode}"\n', 1)
    path.write_text(text, encoding='utf-8')
    return mode
