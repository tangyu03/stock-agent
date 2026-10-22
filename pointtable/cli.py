from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db.session import get_connection, init_db, set_database_path
from .jobs.cutover import expire_legacy_signals, formal_push_ready, switch_push_mode
from .core.sector import load_theme_map
from .core.auction import AuctionPattern
from .jobs.routines import run_auction, run_evening, run_restore, run_settle, run_watch
from .jobs.shadow import run_shadow
from .models.receipt import ReceiptEntry, ReceiptVerdict
from .stats import behavior, pool_quality, signal_quality, stop_outcomes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def add_receipt(
    signal_id: str,
    verdict: str,
    size_filled: int | None = None,
    created_at: str | None = None,
) -> int:
    entry = ReceiptEntry(
        signal_id=signal_id,
        verdict=ReceiptVerdict(verdict),
        size_filled=size_filled,
        created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(timezone.utc),
    )
    conn = get_connection()
    cursor = conn.execute(
        'INSERT OR IGNORE INTO receipts (signal_id, verdict, size_filled, created_at) VALUES (?,?,?,?)',
        (entry.signal_id, entry.verdict.value, entry.size_filled, entry.created_at.isoformat(timespec='seconds')),
    )
    conn.commit()
    return cursor.rowcount


def list_holdings() -> list[sqlite3.Row]:
    conn = get_connection()
    return conn.execute(
        '''SELECT symbol, MIN(date) AS first_date, MAX(date) AS last_date,
                  GROUP_CONCAT(DISTINCT theme_label) AS themes
           FROM pool GROUP BY symbol ORDER BY MAX(date) DESC, symbol'''
    ).fetchall()


def list_pool() -> list[sqlite3.Row]:
    return get_connection().execute(
        'SELECT date, symbol, theme_label FROM pool ORDER BY date DESC, symbol'
    ).fetchall()


def stats_report() -> dict:
    conn = get_connection()
    signals = [dict(row) for row in conn.execute('SELECT id, strategy_id, tag, status FROM signals')]
    confirmed = {
        row['signal_id']
        for row in conn.execute(
            "SELECT DISTINCT signal_id FROM journal WHERE to_status='TRIGGERED' AND confirmed='confirmed'"
        )
    }
    for row in signals:
        row['confirmed'] = row['id'] in confirmed
        row['verdict'] = None
    receipts = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts')]
    receipt_by_signal = {row['signal_id']: row['verdict'] for row in reversed(receipts)}
    for row in signals:
        row['verdict'] = receipt_by_signal.get(row['id'])
    pool_rows = [dict(row) for row in conn.execute('SELECT symbol, theme_label FROM pool GROUP BY symbol')]
    return {
        'signal_quality': signal_quality(signals),
        'stop_outcomes': stop_outcomes(signals),
        'behavior': behavior(signals),
        'pool_quality': pool_quality(pool_rows, []),
    }


def legacy_signals() -> list[sqlite3.Row]:
    return get_connection().execute(
        '''SELECT id, symbol, strategy_id, status, status_reason, closed_at
           FROM signals
           WHERE status_reason='system_migration'
           ORDER BY closed_at DESC, id'''
    ).fetchall()


def audit_pool_themes(theme_map_path: Path | None = None) -> dict:
    mapping = load_theme_map(
        theme_map_path or Path(__file__).parents[0] / 'config' / 'theme_map.yaml'
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pointtable')
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--legacy', action='store_true', help='query system migration audit rows')
    commands = parser.add_subparsers(dest='command')
    receipt = commands.add_parser('receipt')
    receipt.add_argument('signal_id')
    receipt.add_argument('--verdict', choices=[item.value for item in ReceiptVerdict], required=True)
    receipt.add_argument('--size-filled', type=int)
    veto = commands.add_parser('veto')
    veto.add_argument('signal_id')
    commands.add_parser('stats')
    commands.add_parser('holdings')
    commands.add_parser('pool')
    shadow = commands.add_parser('shadow-run')
    shadow.add_argument('--date', required=True)
    shadow.add_argument('--expected-id', action='append', default=[])
    shadow.add_argument('--push-test', action='store_true')
    cutover = commands.add_parser('cutover')
    cutover.add_argument('--yes', action='store_true')
    cutover.add_argument('--now')
    auction = commands.add_parser('auction')
    auction.add_argument('--date', required=True)
    auction.add_argument('--pattern', choices=[item.value for item in AuctionPattern], required=True)
    watch = commands.add_parser('watch')
    watch.add_argument('--price', type=float, required=True)
    watch.add_argument('--second-cycle', action='store_true')
    watch.add_argument('--volume-ratio', type=float)
    watch.add_argument('--date')
    settle = commands.add_parser('settle')
    settle.add_argument('--now')
    restore = commands.add_parser('restore')
    restore.add_argument('--close-price', type=float, required=True)
    evening = commands.add_parser('evening')
    evening.add_argument('--input', type=Path, required=True)
    commands.add_parser('legacy')
    commands.add_parser('pool-audit')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.command and not args.legacy:
        raise SystemExit('pointtable: error: a command is required')
    if args.db is not None:
        set_database_path(args.db)
    init_db()
    if args.legacy:
        print(json.dumps([dict(row) for row in legacy_signals()], ensure_ascii=False, indent=2))
        return 0
    if args.command in {'receipt', 'veto'}:
        verdict = 'veto' if args.command == 'veto' else args.verdict
        inserted = add_receipt(args.signal_id, verdict, getattr(args, 'size_filled', None), _now())
        print(json.dumps({'signal_id': args.signal_id, 'verdict': verdict, 'inserted': inserted}, ensure_ascii=False))
    elif args.command == 'stats':
        print(json.dumps(stats_report(), ensure_ascii=False, indent=2))
    elif args.command == 'holdings':
        print(json.dumps([dict(row) for row in list_holdings()], ensure_ascii=False, indent=2))
    elif args.command == 'pool':
        print(json.dumps([dict(row) for row in list_pool()], ensure_ascii=False, indent=2))
    elif args.command == 'shadow-run':
        result = run_shadow(get_connection(), args.date, args.expected_id)
        if args.push_test:
            from .jobs.shadow import test_channel_sender

            sender = test_channel_sender()
            pushed = sender.send(
                'Pointtable shadow run',
                json.dumps(result.__dict__, ensure_ascii=False, indent=2),
            )
            result = {**result.__dict__, 'pushed': pushed}
        else:
            result = result.__dict__
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'cutover':
        if not args.yes:
            raise SystemExit('cutover requires --yes')
        if not formal_push_ready():
            raise SystemExit('formal push token is not configured')
        switch_push_mode()
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        result = expire_legacy_signals(get_connection(), now)
        get_connection().commit()
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == 'legacy':
        print(json.dumps([dict(row) for row in legacy_signals()], ensure_ascii=False, indent=2))
    elif args.command == 'pool-audit':
        print(json.dumps(audit_pool_themes(), ensure_ascii=False, indent=2))
    elif args.command == 'auction':
        now = datetime.now(timezone.utc)
        print(json.dumps(run_auction(get_connection(), args.date, AuctionPattern(args.pattern), now), ensure_ascii=False, indent=2))
    elif args.command == 'watch':
        print(json.dumps(run_watch(
            get_connection(), args.price, args.second_cycle,
            datetime.now(timezone.utc), args.volume_ratio, args.date,
        ), ensure_ascii=False, indent=2))
    elif args.command == 'settle':
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        print(json.dumps(run_settle(get_connection(), now), ensure_ascii=False, indent=2))
    elif args.command == 'restore':
        print(json.dumps(run_restore(get_connection(), args.close_price), ensure_ascii=False, indent=2))
    elif args.command == 'evening':
        print(json.dumps(run_evening(get_connection(), args.input), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
