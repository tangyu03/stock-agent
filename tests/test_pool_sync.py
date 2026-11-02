"""pool-sync：config/pool.yaml 声明式池子 → pool 表当日快照。"""

import pytest

from pointtable.cli import (
    POOL_CONFIG_PATH,
    add_pool_entry,
    list_pool,
    main,
    sync_pool_from_config,
)
from pointtable.db.session import init_db, set_database_path


def _write_config(path, text: str):
    path.write_text(text, encoding='utf-8')
    return path


def _pool_rows():
    return [(row['symbol'], row['theme_label'], row['role']) for row in list_pool()]


def test_pool_sync_creates_snapshot_via_cli(tmp_path, monkeypatch, tmp_db):
    set_database_path(tmp_db)
    init_db()
    cfg = _write_config(tmp_path / 'pool.yaml', (
        'pool:\n'
        '  - symbol: 2975\n'
        '    theme: 3C设备\n'
        '  - symbol: 920045\n'
    ))
    monkeypatch.setattr('pointtable.cli.POOL_CONFIG_PATH', cfg)
    assert main(['--db', str(tmp_db), 'pool-sync', '--date', '2026-01-02']) == 0
    # 2975 补齐为 002975；920045 无 theme，走真实 theme_map 兜底（光模块CPO）
    assert set(_pool_rows()) == {
        ('002975', '3C设备', 'holding'),
        ('920045', '光模块CPO', 'holding'),
    }


def test_pool_sync_diff_and_removal_rewrites_snapshot(tmp_path, tmp_db):
    set_database_path(tmp_db)
    init_db()
    for symbol, theme in (('300001', 'AI'), ('300002', '机器人'), ('300003', '芯片')):
        add_pool_entry('2026-01-01', symbol, theme)
    cfg = _write_config(tmp_path / 'pool.yaml', (
        'pool:\n'
        '  - symbol: 300001\n'
        '    theme: AI\n'
        '  - symbol: 300002\n'
        '    theme: 机器人\n'
        '    role: candidate\n'
        '  - symbol: 300004\n'
        '    theme: 半导体设备\n'
    ))
    summary = sync_pool_from_config('2026-01-01', config_path=cfg)
    assert summary == {
        'date': '2026-01-01',
        'total': 3,
        'added': ['300004'],
        'removed': ['300003'],
        'changed': ['300002'],
        'kept': ['300001'],
    }
    symbols = {row['symbol']: row['role'] for row in list_pool()}
    assert symbols == {'300001': 'holding', '300002': 'candidate', '300004': 'holding'}


def test_pool_sync_same_date_is_idempotent(tmp_path, tmp_db):
    set_database_path(tmp_db)
    init_db()
    cfg = _write_config(tmp_path / 'pool.yaml', (
        'pool:\n'
        '  - symbol: 300001\n'
        '    theme: AI\n'
    ))
    first = sync_pool_from_config('2026-01-01', config_path=cfg)
    rows_after_first = _pool_rows()
    second = sync_pool_from_config('2026-01-01', config_path=cfg)
    assert first['added'] == ['300001']
    assert second == {
        'date': '2026-01-01',
        'total': 1,
        'added': [],
        'removed': [],
        'changed': [],
        'kept': ['300001'],
    }
    assert _pool_rows() == rows_after_first


def test_pool_sync_refuses_empty_pool(tmp_path, tmp_db):
    set_database_path(tmp_db)
    init_db()
    add_pool_entry('2026-01-01', '300001', 'AI')
    before = _pool_rows()
    empty_list = _write_config(tmp_path / 'empty.yaml', 'pool: []\n')
    with pytest.raises(ValueError):
        sync_pool_from_config('2026-01-02', config_path=empty_list)
    missing_key = _write_config(tmp_path / 'nokey.yaml', 'watchlist: 25\n')
    with pytest.raises(ValueError):
        sync_pool_from_config('2026-01-02', config_path=missing_key)
    assert _pool_rows() == before


def test_pool_sync_rejects_invalid_entries(tmp_path, tmp_db):
    set_database_path(tmp_db)
    init_db()
    add_pool_entry('2026-01-01', '300001', 'AI')
    before = _pool_rows()

    bad_role = _write_config(tmp_path / 'role.yaml', (
        'pool:\n'
        '  - symbol: 300002\n'
        '    theme: AI\n'
        '    role: position\n'
    ))
    with pytest.raises(ValueError, match='role'):
        sync_pool_from_config('2026-01-02', config_path=bad_role)

    duplicate = _write_config(tmp_path / 'dup.yaml', (
        'pool:\n'
        '  - symbol: 2975\n'
        '    theme: AI\n'
        '  - symbol: 002975\n'
        '    theme: 机器人\n'
    ))
    with pytest.raises(ValueError, match='duplicate'):
        sync_pool_from_config('2026-01-02', config_path=duplicate)

    no_symbol = _write_config(tmp_path / 'nosym.yaml', (
        'pool:\n'
        '  - name: 博杰股份\n'
    ))
    with pytest.raises(ValueError, match='missing symbol'):
        sync_pool_from_config('2026-01-02', config_path=no_symbol)

    with pytest.raises(ValueError, match='invalid pool sync date'):
        sync_pool_from_config('2026-1-2', config_path=_write_config(tmp_path / 'ok.yaml', (
            'pool:\n'
            '  - symbol: 300002\n'
            '    theme: AI\n'
        )))
    assert _pool_rows() == before


def test_pool_sync_theme_resolution_order(tmp_path, tmp_db):
    set_database_path(tmp_db)
    init_db()
    theme_map = _write_config(tmp_path / 'theme_map.yaml', (
        'overrides:\n'
        '  "600001": AI\n'
        '  "600002": AI\n'
    ))
    cfg = _write_config(tmp_path / 'pool.yaml', (
        'pool:\n'
        '  - symbol: 600001\n'
        "    theme: 'unknown'\n"
        '  - symbol: 600002\n'
        "    theme: ''\n"
        '  - symbol: 600003\n'
    ))
    sync_pool_from_config('2026-01-01', config_path=cfg, theme_map_path=theme_map)
    themes = {row['symbol']: row['theme_label'] for row in list_pool()}
    # 显式 'unknown' 原样生效 > theme_map 兜底；空串走兜底；无映射兜底为 unknown
    assert themes == {'600001': 'unknown', '600002': 'AI', '600003': 'unknown'}


def test_pool_sync_warns_on_backdated_and_future_dates(tmp_path, tmp_db, capsys):
    set_database_path(tmp_db)
    init_db()
    cfg = _write_config(tmp_path / 'pool.yaml', (
        'pool:\n'
        '  - symbol: 300001\n'
        '    theme: AI\n'
    ))
    add_pool_entry('2026-01-05', '300002', '机器人')
    sync_pool_from_config('2026-01-01', config_path=cfg)
    captured = capsys.readouterr()
    assert 'warning' in captured.err
    assert '2026-01-01' in captured.err and '2026-01-05' in captured.err
    sync_pool_from_config('2099-01-01', config_path=cfg)
    captured = capsys.readouterr()
    assert 'future' in captured.err


def test_pool_sync_missing_config_file(tmp_path, monkeypatch, tmp_db):
    set_database_path(tmp_db)
    init_db()
    monkeypatch.setattr('pointtable.cli.POOL_CONFIG_PATH', tmp_path / 'nope.yaml')
    with pytest.raises(ValueError, match='not found'):
        main(['--db', str(tmp_db), 'pool-sync', '--date', '2026-01-02'])
    assert POOL_CONFIG_PATH.name == 'pool.yaml'
