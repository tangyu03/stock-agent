import json
from datetime import UTC, datetime
from pathlib import Path

from pointtable.cli import list_holdings, list_pool, main, stats_report
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


def test_pool_add_and_role_filters(tmp_db):
    set_database_path(tmp_db)
    init_db()
    assert main(['--db', str(tmp_db), 'pool-add', '--date', '2026-01-01', '--symbol', '300001', '--theme', 'AI']) == 0
    assert main(['--db', str(tmp_db), 'pool-add', '--date', '2026-01-01', '--symbol', '300002', '--theme', '机器人', '--role', 'candidate']) == 0
    assert [row['symbol'] for row in list_holdings()] == ['300001', '300002']
    assert [row['role'] for row in list_pool()] == ['holding', 'candidate']


def test_push_file_is_manual_one_way(monkeypatch, tmp_db):
    sent = []

    class FakeSender:
        def __init__(self, channel=None):
            self.channel = channel
        def send(self, title, content):
            sent.append((title, content, self.channel))
            return True

    monkeypatch.setattr('pointtable.cli.PushPlusSender', FakeSender)
    report_path = Path.cwd() / 'test_push_file.md'
    report_path.write_text('report body', encoding='utf-8')
    try:
        assert main([
            '--db', str(tmp_db), 'push-file', '--title', '复盘',
            '--file', str(report_path), '--channel', 'test',
        ]) == 0
    finally:
        report_path.unlink(missing_ok=True)
    assert sent == [('复盘', 'report body', 'test')]
