import json
from datetime import UTC, datetime
from pathlib import Path

from pointtable.cli import main
from pointtable.db.session import get_connection, init_db, set_database_path
from pointtable.jobs.cutover import expire_legacy_signals
from pointtable.jobs.shadow import run_shadow


def _prepare(db_path: Path) -> None:
    set_database_path(db_path)
    init_db()
    conn = get_connection()
    conn.execute(
        '''INSERT INTO signals (
            id, symbol, strategy_id, side, tag, zone_low, zone_high, premise,
            trend, valid_policy, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('2026-01-01-300001-first', '300001', 'first_move_5d', 'BUY', 'enter', 10.0, 10.2, '{}', 'UP', 'intraday', 'ACTIVE', '2026-01-01T00:00:00+00:00'),
    )
    conn.commit()


def test_shadow_run_reports_missing_and_unexpected(tmp_db):
    _prepare(tmp_db)
    result = run_shadow(
        get_connection(),
        '2026-01-01',
        ['2026-01-01-300001-first', '2026-01-01-300001-missed'],
    )
    assert result.ok is False
    assert result.missed_ids == ['2026-01-01-300001-missed']
    assert result.unexpected_ids == []
    assert result.matched_ids == ['2026-01-01-300001-first']


def test_cutover_expires_legacy_signals_once(tmp_db):
    _prepare(tmp_db)
    conn = get_connection()
    conn.execute(
        '''INSERT INTO signals (
            id, symbol, strategy_id, side, tag, zone_low, zone_high, premise,
            trend, valid_policy, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('old-triggered', '300002', 'first_move_5d', 'BUY', 'enter', 10.0, 10.2, '{}', 'UP', 'intraday', 'TRIGGERED', '2025-12-31T00:00:00+00:00'),
    )
    conn.execute(
        '''INSERT INTO signals (
            id, symbol, strategy_id, side, tag, zone_low, zone_high, premise,
            trend, valid_policy, status, status_reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('old-expired', '300003', 'first_move_5d', 'BUY', 'enter', 10.0, 10.2, '{}', 'UP', 'intraday', 'EXPIRED', 'manual', '2025-12-30T00:00:00+00:00'),
    )
    conn.commit()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    result = expire_legacy_signals(conn, now)
    conn.commit()
    assert result.expired_count == 2
    assert result.journal_count == 2
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE status='ACTIVE'").fetchone()[0] == 0
    rerun = expire_legacy_signals(conn, now)
    assert rerun.expired_count == 0
    assert rerun.journal_count == 0
    main(['legacy'])
