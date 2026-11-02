"""池子管理与审计命令的实现，从 cli.py 迁出（批次3）。

list_holdings / list_pool / add_pool_entry / sync_pool_from_config / audit_pool_themes
全部只碰 pool 表与配置文件，不含 argparse——cli 只做参数解析与 JSON 打印。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml

from ..core.sector import load_theme_map
from ..db.session import get_connection


def list_holdings() -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        '''SELECT symbol, MIN(date) AS first_date, MAX(date) AS last_date,
                  GROUP_CONCAT(DISTINCT theme_label) AS themes
           FROM pool
           GROUP BY symbol ORDER BY MAX(date) DESC, symbol'''
    ).fetchall()


def list_pool() -> list[sqlite3.Row]:
    return get_connection().execute(
        'SELECT date, symbol, theme_label, role FROM pool ORDER BY date DESC, symbol'
    ).fetchall()


def add_pool_entry(
    trade_date: str,
    symbol: str,
    theme_label: str,
    role: str = 'holding',
) -> int:
    if role not in {'holding', 'index', 'sector', 'candidate'}:
        raise ValueError('pool role must be holding, index, sector or candidate')
    conn = get_connection()
    cursor = conn.execute(
        'INSERT OR IGNORE INTO pool (date, symbol, theme_label, role) VALUES (?,?,?,?)',
        (trade_date, symbol, theme_label, role),
    )
    conn.commit()
    return cursor.rowcount


POOL_CONFIG_PATH = Path(__file__).parents[2] / 'config' / 'pool.yaml'


def _normalize_pool_symbol(value: object) -> str:
    symbol = str(value).strip()
    if symbol.isdigit():
        symbol = symbol.zfill(6)
    return symbol


def sync_pool_from_config(
    sync_date: str,
    config_path: Path | None = None,
    theme_map_path: Path | None = None,
) -> dict:
    '''把 config/pool.yaml 的全量声明落成 sync_date 当日的池子快照。

    当日行先 DELETE 后 INSERT（单事务），历史日期永不改写；
    与运行时 reader 同口径取基线：date = MAX(date) <= sync_date。
    '''
    try:
        date.fromisoformat(sync_date)
    except ValueError as exc:
        raise ValueError(f'invalid pool sync date {sync_date!r}: expected YYYY-MM-DD') from exc

    pool_path = Path(config_path) if config_path else POOL_CONFIG_PATH
    if not pool_path.exists():
        raise ValueError(f'pool config not found: {pool_path}')
    raw = yaml.safe_load(pool_path.read_text(encoding='utf-8')) or {}
    if not isinstance(raw, dict):
        raise ValueError('pool config must be a mapping with a pool list')
    entries = raw.get('pool')
    if not isinstance(entries, list) or not entries:
        raise ValueError('pool config must contain a non-empty pool list')

    valid_roles = {'holding', 'index', 'sector', 'candidate'}
    desired: dict[str, tuple[str, str]] = {}
    mapping: dict[str, str] | None = None
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f'pool entry #{index + 1} must be a mapping')
        raw_symbol = item.get('symbol')
        if raw_symbol is None or str(raw_symbol).strip() == '':
            raise ValueError(f'pool entry #{index + 1} is missing symbol')
        symbol = _normalize_pool_symbol(raw_symbol)
        if symbol in desired:
            raise ValueError(f'duplicate symbol in pool config: {symbol}')
        theme = str(item.get('theme') or '').strip()
        if not theme:
            if mapping is None:
                mapping = load_theme_map(
                    theme_map_path
                    or Path(__file__).parents[2] / 'config' / 'theme_map.yaml'
                )
            theme = mapping.get(symbol, '') or 'unknown'
        role = str(item.get('role') or '').strip() or 'holding'
        if role not in valid_roles:
            raise ValueError(f'pool role for {symbol} must be holding, index, sector or candidate')
        desired[symbol] = (theme, role)

    conn = get_connection()
    base_rows = conn.execute(
        '''
        SELECT symbol, theme_label, role FROM pool
        WHERE date = (SELECT MAX(date) FROM pool WHERE date <= ?)
        ''',
        (sync_date,),
    ).fetchall()
    base_by_symbol: dict[str, set[tuple[str, str]]] = {}
    for row in base_rows:
        base_by_symbol.setdefault(str(row['symbol']), set()).add(
            (str(row['theme_label']), str(row['role']))
        )

    added = sorted(s for s in desired if s not in base_by_symbol)
    removed = sorted(s for s in base_by_symbol if s not in desired)
    changed = sorted(
        s for s in desired
        if s in base_by_symbol and base_by_symbol[s] != {desired[s]}
    )
    kept = sorted(
        s for s in desired
        if s in base_by_symbol and base_by_symbol[s] == {desired[s]}
    )

    latest_row = conn.execute('SELECT MAX(date) AS latest FROM pool').fetchone()
    latest = latest_row['latest'] if latest_row else None
    if latest and sync_date < latest:
        print(
            f'pool-sync warning: {sync_date} is before the latest pool snapshot {latest}; '
            f'the historical snapshot at {sync_date} will be rewritten',
            file=sys.stderr,
        )
    if sync_date > date.today().isoformat():
        print(
            f'pool-sync warning: {sync_date} is in the future; the snapshot becomes '
            f'visible to runtime readers only from that date onward',
            file=sys.stderr,
        )

    try:
        conn.execute('DELETE FROM pool WHERE date=?', (sync_date,))
        conn.executemany(
            'INSERT INTO pool (date, symbol, theme_label, role) VALUES (?,?,?,?)',
            [(sync_date, symbol, theme, role) for symbol, (theme, role) in desired.items()],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        'date': sync_date,
        'total': len(desired),
        'added': added,
        'removed': removed,
        'changed': changed,
        'kept': kept,
    }


def audit_pool_themes(theme_map_path: Path | None = None) -> dict:
    mapping = load_theme_map(
        theme_map_path or Path(__file__).parents[2] / 'config' / 'theme_map.yaml'
    )
    rows = get_connection().execute(
        '''SELECT symbol, GROUP_CONCAT(DISTINCT theme_label) AS pool_theme
           FROM pool GROUP BY symbol ORDER BY symbol'''
    ).fetchall()
    audited = []
    for row in rows:
        mapped_theme = mapping.get(row['symbol'], '')
        if not mapped_theme:
            status = 'unmapped'
        elif row['pool_theme'] not in ('', 'unknown') and mapped_theme not in row['pool_theme'].split(','):
            status = 'mismatch'
        else:
            status = 'mapped'
        audited.append({
            'symbol': row['symbol'],
            'pool_theme': row['pool_theme'],
            'theme_map': mapped_theme,
            'status': status,
        })
    counts = {status: sum(row['status'] == status for row in audited) for status in ('mapped', 'mismatch', 'unmapped')}
    return {'total': len(audited), 'counts': counts, 'rows': audited}
