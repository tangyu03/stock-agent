from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ShadowRunResult:
    trade_date: str
    expected_ids: list[str]
    actual_ids: list[str]
    matched_ids: list[str]
    missed_ids: list[str]
    unexpected_ids: list[str]
    ok: bool


def evaluate_shadow_day(
    expected_ids: list[str],
    actual_ids: list[str],
    trade_date: str,
) -> ShadowRunResult:
    expected = set(expected_ids)
    actual = set(actual_ids)
    matched = sorted(expected & actual)
    missed = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    return ShadowRunResult(
        trade_date=trade_date,
        expected_ids=sorted(expected),
        actual_ids=sorted(actual),
        matched_ids=matched,
        missed_ids=missed,
        unexpected_ids=unexpected,
        ok=not missed and not unexpected,
    )


def actual_signal_ids(conn: sqlite3.Connection, trade_date: str) -> list[str]:
    rows = conn.execute(
        'SELECT id FROM signals WHERE created_at LIKE ? ORDER BY id',
        (trade_date + '%',),
    ).fetchall()
    return [row['id'] for row in rows]


def run_shadow(
    conn: sqlite3.Connection,
    trade_date: str,
    expected_ids: list[str],
    actual_ids: list[str] | None = None,
) -> ShadowRunResult:
    if actual_ids is None:
        actual_ids = actual_signal_ids(conn, trade_date)
    return evaluate_shadow_day(expected_ids, actual_ids, trade_date)


def test_channel_sender(config_path: str | Path | None = None):
    from ..notify.push import PushPlusSender

    path = config_path or Path(__file__).parents[1] / 'config' / 'push.yaml'
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    token = str((raw.get('pushplus') or {}).get('test_token') or '')
    if not token:
        raise RuntimeError('test push token is not configured')
    return PushPlusSender(config_path, token=token)
