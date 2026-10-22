from pathlib import Path

import pytest
import yaml
from unittest.mock import patch

from pointtable.cli import audit_pool_themes
from pointtable.cli import main
from pointtable.db.session import get_connection, init_db, set_database_path
from pointtable.jobs.cutover import push_mode, switch_push_mode
from pointtable.notify.push import PushPlusSender


def test_push_mode_uses_shadow_token():
    path = Path.cwd() / 'test_push_mode.yaml'
    path.write_text(
        'pushplus:\n  mode: shadow\n  token: formal\n  test_token: test\n  daily_limit: 1\n',
        encoding='utf-8',
    )
    try:
        assert PushPlusSender(path).token == 'test'
        assert PushPlusSender(path, token='override').token == 'override'
    finally:
        path.unlink(missing_ok=True)


def test_switch_push_mode_updates_without_mode_field():
    path = Path.cwd() / 'test_push_switch.yaml'
    path.write_text('pushplus:\n  token: abc\n', encoding='utf-8')
    try:
        assert switch_push_mode(path, 'formal') == 'formal'
        assert push_mode(path) == 'formal'
        assert yaml.safe_load(path.read_text(encoding='utf-8'))['pushplus']['token'] == 'abc'
    finally:
        path.unlink(missing_ok=True)


def test_cutover_does_not_switch_without_formal_token(tmp_db):
    path = Path.cwd() / 'test_push_cutover_guard.yaml'
    path.write_text('pushplus:\n  mode: shadow\n  test_token: test\n', encoding='utf-8')
    try:
        with patch(
            'pointtable.cli.formal_push_ready',
            return_value=False,
        ) as ready:
            with pytest.raises(SystemExit):
                main(['--db', str(tmp_db), 'cutover', '--yes'])
        assert ready.call_count == 1
        assert push_mode(path) == 'shadow'
    finally:
        path.unlink(missing_ok=True)


def test_pool_theme_audit(tmp_db):
    set_database_path(tmp_db)
    init_db()
    conn = get_connection()
    conn.executemany(
        'INSERT INTO pool (date, symbol, theme_label) VALUES (?,?,?)',
        [('2026-01-01', '300001', 'AI'), ('2026-01-01', '999999', 'unknown')],
    )
    conn.commit()
    mapping_path = Path.cwd() / 'test_theme_map.yaml'
    mapping_path.write_text('overrides:\n  300001: AI\n  999999: 破位观察\n', encoding='utf-8')
    try:
        report = audit_pool_themes(mapping_path)
        assert report['counts'] == {'mapped': 2, 'mismatch': 0, 'unmapped': 0}
        assert report['rows'][0]['status'] == 'mapped'
        assert report['rows'][1]['status'] == 'mapped'
    finally:
        set_database_path(Path('unused'))
        mapping_path.unlink(missing_ok=True)


def test_legacy_top_level_query(tmp_db):
    set_database_path(tmp_db)
    init_db()
    assert main(['--db', str(tmp_db), '--legacy']) == 0
