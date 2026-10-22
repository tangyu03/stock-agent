import json
from datetime import UTC, datetime
from pathlib import Path

from pointtable.cli import main, stats_report
from pointtable.db.session import get_connection, init_db, set_database_path


def _prepare(db_path: Path) -> None:
    set_database_path(db_path)
    init_db()
    conn = get_connection()
    conn.execute(
        '''INSERT INTO signals (
            id, symbol, strategy_id, side, tag, zone_low, zone_high, premise,
            trend, valid_policy, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('evt-1', '300001', 'strategy-a', 'BUY', 'enter', 10.0, 10.2, '{}', 'UP', 'intraday', 'TRIGGERED', '2026-01-01T00:00:00+00:00'),
    )
    conn.execute(
        'INSERT INTO journal (signal_id, from_status, to_status, reason, price, confirmed, created_at) VALUES (?,?,?,?,?,?,?)',
        ('evt-1', 'ACTIVE', 'TRIGGERED', 'hit', 10.1, 'confirmed', '2026-01-01T10:00:00+00:00'),
    )
    conn.execute('INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)', ('2026-01-01', '300001', 'AI'))
    conn.commit()


def test_cli_receipt_stats_holdings_and_pool():
    db_path = Path.cwd() / 'test_cli.db'
    if db_path.exists():
        db_path.unlink()
    try:
        _prepare(db_path)
        main(['--db', str(db_path), 'receipt', 'evt-1', '--verdict', 'executed', '--size-filled', '100'])
        main(['--db', str(db_path), 'veto', 'evt-1'])
        report = stats_report()
        assert report['signal_quality']['strategy-a']['confirmed'] == 1
        assert report['behavior']['receipt_coverage'] == 1.0
        main(['--db', str(db_path), 'holdings'])
        main(['--db', str(db_path), 'pool'])
    finally:
        set_database_path(Path('unused'))
        db_path.unlink(missing_ok=True)
