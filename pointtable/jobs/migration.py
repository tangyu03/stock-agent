from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from ..core.sector import load_theme_map
from .cutover import formal_push_ready, push_mode


MIGRATION_REASON = 'system_migration'
STABLE_DAYS = 7
LEGACY_RETENTION_DAYS = 30


@dataclass(frozen=True)
class MigrationStatus:
    pushed: dict
    audit: dict
    checks: dict[str, bool]
    ready: bool


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
        ZoneInfo('Asia/Shanghai')
    ).date()


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(value.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def migration_status(
    conn: sqlite3.Connection,
    theme_map_path: str | Path | None = None,
    push_config_path: str | Path | None = None,
    today: date | None = None,
) -> dict:
    audit_row = conn.execute(
        '''SELECT COUNT(*) AS count, MIN(created_at) AS cutover_at
           FROM journal WHERE reason=?''',
        (MIGRATION_REASON,),
    ).fetchone()
    cutover_at = audit_row['cutover_at']
    cutover_on = _as_date(cutover_at) if cutover_at else None
    current = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    elapsed_days = (current - cutover_on).days if cutover_on else None
    target_on = cutover_on + timedelta(days=STABLE_DAYS) if cutover_on else None
    retention_on = _add_months(cutover_on, 1) if cutover_on else None

    mapping = load_theme_map(
        Path(theme_map_path)
        if theme_map_path
        else Path(__file__).parents[1] / 'config' / 'theme_map.yaml'
    )
    pool_rows = conn.execute(
        '''SELECT symbol, GROUP_CONCAT(DISTINCT theme_label) AS pool_theme
           FROM pool GROUP BY symbol ORDER BY symbol'''
    ).fetchall()
    audited = []
    for row in pool_rows:
        mapped_theme = mapping.get(row['symbol'], '')
        if not mapped_theme:
            status = 'unmapped'
        elif row['pool_theme'] not in ('', 'unknown') and mapped_theme not in row['pool_theme'].split(','):
            status = 'mismatch'
        else:
            status = 'mapped'
        if status != 'mapped':
            audited.append({
                'symbol': row['symbol'],
                'pool_theme': row['pool_theme'],
                'theme_map': mapped_theme,
                'status': status,
            })
    counts = {
        status: sum(row['status'] == status for row in audited)
        for status in ('mapped', 'mismatch', 'unmapped')
    }
    counts['mapped'] = len(pool_rows) - counts['mismatch'] - counts['unmapped']

    legacy_active = conn.execute(
        '''SELECT COUNT(*) FROM signals
           WHERE status IN ('ACTIVE','TRIGGERED') AND status_reason=?''',
        (MIGRATION_REASON,),
    ).fetchone()[0]
    mode = push_mode(push_config_path)
    token_ready = formal_push_ready(push_config_path)
    checks = {
        'push_mode_formal': mode == 'formal',
        'formal_token_ready': token_ready,
        'migration_journal_present': bool(audit_row['count']),
        'legacy_active_signals_expired': legacy_active == 0,
        'pool_theme_audit_clean': counts['mismatch'] == 0 and counts['unmapped'] == 0,
    }
    return {
        'cutover': {
            'at': cutover_at,
            'migration_journal_count': audit_row['count'],
            'legacy_active_count': legacy_active,
        },
        'stable_period': {
            'started_on': cutover_on.isoformat() if cutover_on else None,
            'target_on': target_on.isoformat() if target_on else None,
            'days_completed': elapsed_days,
            'days_remaining': max(0, STABLE_DAYS - elapsed_days) if elapsed_days is not None else STABLE_DAYS,
            'complete': elapsed_days is not None and elapsed_days >= STABLE_DAYS,
        },
        'legacy_retention_until': retention_on.isoformat() if retention_on else None,
        'push': {
            'mode': mode,
            'formal_token_ready': token_ready,
        },
        'pool_audit': {
            'total': len(pool_rows),
            'counts': counts,
            'problem_rows': audited,
        },
        'checks': checks,
        'ready': all(checks.values()),
    }
