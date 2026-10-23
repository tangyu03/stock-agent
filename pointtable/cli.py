from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .db.session import (
    get_connection,
    init_db,
    load_event_calendar,
    set_database_path,
)
from .jobs.cutover import expire_legacy_signals, formal_push_ready, switch_push_mode
from .core.generator import TSizeChoice, apply_t_size_choice
from .core.sector import load_theme_map
from .core.calendar import load_holidays
from .core.auction import AuctionPattern
from .jobs.routines import run_auction, run_evening, run_restore, run_settle, run_watch
from .jobs.execution_review import today_execution_review
from .jobs.market_pipeline import fetch_market_input
from .jobs.market_pipeline import load_risk_config
from .jobs.scheduler import next_routine
from .jobs.tail_snapshot import capture_tail_snapshots
from .jobs.tail_snapshot import load_tail_prices
from .jobs.migration import migration_status
from .jobs.shadow import run_shadow
from .jobs.evening import evening_report
from .models.receipt import ReceiptEntry, ReceiptVerdict
from .stats import behavior, correction_hit_rate, pool_quality, signal_quality, stop_outcomes
from .notify.push import PushPlusSender


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


def stats_report(performance: dict | None = None) -> dict:
    conn = get_connection()
    signals = [dict(row) for row in conn.execute('SELECT id, strategy_id, tag, status FROM signals')]
    receipts = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts')]
    receipt_by_signal = {row['signal_id']: row['verdict'] for row in reversed(receipts)}
    for row in signals:
        row['verdict'] = receipt_by_signal.get(row['id'])
    pool_rows = [dict(row) for row in conn.execute('SELECT symbol, theme_label FROM pool GROUP BY symbol')]
    behavior_data = behavior(signals)
    if performance:
        behavior_data['restore_violations'] = (performance.get('restore') or {}).get('violations')
    return {
        'signal_quality': signal_quality(signals),
        'correction_hit_rate': performance.get('correction_hit_rate') if performance else None,
        'stop_outcomes': stop_outcomes(signals),
        'behavior': behavior_data,
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


def build_report(
    report_date: str,
    market: dict[str, Any] | None = None,
) -> str:
    from .jobs.routines import signal_from_row
    from .notify.render import render_report

    signal_rows = [
        signal_from_row(row)
        for row in get_connection().execute(
            '''
            SELECT * FROM signals
            WHERE created_at LIKE ? OR (closed_at IS NOT NULL AND closed_at LIKE ?)
            ORDER BY created_at, id
            ''',
            (report_date + '%', report_date + '%'),
        ).fetchall()
    ]
    portfolio_path = Path(__file__).parents[1] / 'legacy' / 'config' / 'portfolio.yaml'
    portfolio = yaml.safe_load(portfolio_path.read_text(encoding='utf-8')) or {}
    name_by_symbol = {
        str(item.get('code')): str(item.get('name') or '')
        for item in portfolio.get('stocks') or []
    }
    by_symbol: dict[str, list[Any]] = {}
    for signal in signal_rows:
        by_symbol.setdefault(signal.symbol, []).append(signal)
    market_for_report = dict((market or {}).get('market') or market or {})
    market_for_report['assignments'] = list((market or {}).get('assignments') or [])
    market_for_report['name_map'] = name_by_symbol
    market_for_report['context_marks'] = [
        dict(row) for row in get_connection().execute(
            'SELECT * FROM context_marks WHERE date=? ORDER BY context_type, code',
            (report_date,),
        ).fetchall()
    ]
    market_for_report['event_calendar'] = load_event_calendar(get_connection(), report_date)
    market_for_report['execution_review'] = today_execution_review(get_connection(), report_date)
    market_for_report['ev_negative_strategies'] = sorted({
        signal.strategy_id
        for signals in by_symbol.values()
        for signal in signals
        if signal.premise.get('ev_negative')
    })
    return render_report(report_date, market_for_report, [], signal_rows)


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
    stats = commands.add_parser('stats')
    stats.add_argument('--date')
    stats.add_argument('--window', type=int, default=5)
    commands.add_parser('holdings')
    commands.add_parser('pool')
    pool_add = commands.add_parser('pool-add')
    pool_add.add_argument('--date', required=True)
    pool_add.add_argument('--symbol', required=True)
    pool_add.add_argument('--theme', required=True)
    pool_add.add_argument('--role', choices=['holding', 'index', 'sector', 'candidate'], default='holding')
    shadow = commands.add_parser('shadow-run')
    shadow.add_argument('--date', required=True)
    shadow.add_argument('--expected-id', action='append', default=[])
    shadow.add_argument('--push-test', action='store_true')
    push_file = commands.add_parser('push-file')
    push_file.add_argument('--title', required=True)
    push_file.add_argument('--file', type=Path, required=True)
    push_file.add_argument('--channel', choices=['configured', 'formal', 'test'], default='configured')
    cutover = commands.add_parser('cutover')
    cutover.add_argument('--yes', action='store_true')
    cutover.add_argument('--now')
    auction = commands.add_parser('auction')
    auction.add_argument('--date', required=True)
    auction.add_argument('--pattern', choices=[item.value for item in AuctionPattern], required=True)
    watch = commands.add_parser('watch')
    watch.add_argument('--price', type=float)
    watch.add_argument('--second-cycle', action='store_true')
    watch.add_argument('--volume-ratio', type=float)
    watch.add_argument('--date')
    watch.add_argument('--broken-id', action='append', default=[])
    watch.add_argument('--context-json', type=Path)
    watch.add_argument('--prices-json', type=Path)
    watch.add_argument('--context-prices-json', type=Path)
    watch.add_argument('--retest-volume-min', type=float)
    settle = commands.add_parser('settle')
    settle.add_argument('--now')
    settle.add_argument('--stop-zones-json', type=Path)
    settle.add_argument('--close-prices-json', type=Path)
    settle.add_argument('--market-json', type=Path)
    settle.add_argument('--volume-ratios-json', type=Path)
    settle.add_argument('--break-contexts-json', type=Path)
    settle.add_argument('--retest-window-days', type=int)
    settle.add_argument('--retest-volume-min', type=float)
    settle.add_argument('--holidays-json', type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--close-price', type=float, required=True)
    restore_check = commands.add_parser('restore-check')
    restore_check.add_argument('--date', required=True)
    evening = commands.add_parser('evening')
    evening.add_argument('--input', type=Path, required=True)
    commands.add_parser('next-routine')
    t_size = commands.add_parser('t-size')
    t_size.add_argument('signal_id')
    t_size.add_argument('--choice', choices=[item.value for item in TSizeChoice], required=True)
    report = commands.add_parser('report')
    report.add_argument('--date', required=True)
    report.add_argument('--market-json', type=Path)
    report.add_argument('--output', type=Path)
    market_input = commands.add_parser('market-input')
    tail = commands.add_parser('tail-snapshot')
    tail.add_argument('--date', required=True)
    tail.add_argument('--now')
    market_input.add_argument('--date', required=True)
    market_input.add_argument('--output', type=Path, required=True)
    performance = commands.add_parser('pool-performance')
    performance.add_argument('--date', required=True)
    performance.add_argument('--window', type=int, default=5)
    commands.add_parser('legacy')
    commands.add_parser('pool-audit')
    commands.add_parser('migration-status')
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
        performance = None
        if args.date:
            from .jobs.performance import pool_performance_report

            performance = pool_performance_report(get_connection(), args.date, window=args.window)
        print(json.dumps(stats_report(performance), ensure_ascii=False, indent=2))
    elif args.command == 'holdings':
        print(json.dumps([dict(row) for row in list_holdings()], ensure_ascii=False, indent=2))
    elif args.command == 'pool':
        print(json.dumps([dict(row) for row in list_pool()], ensure_ascii=False, indent=2))
    elif args.command == 'pool-add':
        inserted = add_pool_entry(args.date, args.symbol, args.theme, args.role)
        print(json.dumps({'symbol': args.symbol, 'role': args.role, 'inserted': inserted}, ensure_ascii=False))
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
    elif args.command == 'push-file':
        sender = PushPlusSender(channel=args.channel)
        pushed = sender.send(args.title, args.file.read_text(encoding='utf-8'))
        print(json.dumps({
            'title': args.title,
            'file': str(args.file),
            'channel': args.channel,
            'pushed': pushed,
        }, ensure_ascii=False))
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
    elif args.command == 'migration-status':
        print(json.dumps(migration_status(get_connection()), ensure_ascii=False, indent=2))
    elif args.command == 'auction':
        now = datetime.now(timezone.utc)
        print(json.dumps(run_auction(get_connection(), args.date, AuctionPattern(args.pattern), now), ensure_ascii=False, indent=2))
    elif args.command == 'watch':
        risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        print(json.dumps(run_watch(
            get_connection(), args.price, args.second_cycle,
            datetime.now(timezone.utc), args.volume_ratio, args.date,
            args.broken_id,
            break_context=(
                json.loads(args.context_json.read_text(encoding='utf-8'))
                if args.context_json else None
            ),
            retest_volume_min=(
                args.retest_volume_min
                if args.retest_volume_min is not None
                else risk_config['retest_volume_min']
            ),
            prices=(
                json.loads(args.prices_json.read_text(encoding='utf-8'))
                if args.prices_json else None
            ),
            context_prices=(
                json.loads(args.context_prices_json.read_text(encoding='utf-8'))
                if args.context_prices_json else None
            ),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'settle':
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        zone_path = args.stop_zones_json or args.market_json
        stop_zones = json.loads(zone_path.read_text(encoding='utf-8')) if zone_path else None
        if stop_zones and args.market_json and not args.stop_zones_json:
            stop_zones = stop_zones.get('stop_zones')
        close_prices = json.loads(args.close_prices_json.read_text(encoding='utf-8')) if args.close_prices_json else None
        volume_ratios = json.loads(args.volume_ratios_json.read_text(encoding='utf-8')) if args.volume_ratios_json else None
        break_contexts = json.loads(args.break_contexts_json.read_text(encoding='utf-8')) if args.break_contexts_json else None
        market_payload = json.loads(args.market_json.read_text(encoding='utf-8')) if args.market_json else {}
        market_fields = market_payload.get('market') if isinstance(market_payload.get('market'), dict) else market_payload
        holidays = json.loads(args.holidays_json.read_text(encoding='utf-8')) if args.holidays_json else None
        risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        print(json.dumps(run_settle(
            get_connection(),
            now,
            stop_zones,
            close_prices,
            volume_ratios,
            break_contexts,
            retest_window_days=(
                args.retest_window_days
                if args.retest_window_days is not None
                else risk_config['retest_window_days']
            ),
            retest_volume_min=(
                args.retest_volume_min
                if args.retest_volume_min is not None
                else risk_config['retest_volume_min']
            ),
            holidays=set(holidays) if holidays is not None else risk_config['holidays'],
            market_tier=str(market_fields.get('tier') or 'NORMAL'),
            index_trend=str(market_fields.get('index_trend') or 'UP'),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'restore':
        print(json.dumps(run_restore(get_connection(), args.close_price), ensure_ascii=False, indent=2))
    elif args.command == 'restore-check':
        from .jobs.restore_check import find_unpaired_t_buy

        conn = get_connection()
        t_signals = [signal_from_row(row) for row in conn.execute("SELECT * FROM signals WHERE tag='t'").fetchall()]
        receipt_rows = [dict(row) for row in conn.execute('SELECT signal_id, verdict FROM receipts').fetchall()]
        unpaired, no_receipt, _ = find_unpaired_t_buy(t_signals, receipt_rows, args.date)
        tail_prices = load_tail_prices(conn, args.date)
        signals_by_id = {signal.id: signal for signal in t_signals}
        rows = []
        for item in unpaired:
            signal = signals_by_id[item.signal_id]
            sell = next(
                (
                    row for row in t_signals
                    if row.parent_id == item.parent_id
                    and row.symbol == item.symbol
                    and row.side.value == 'SELL'
                ),
                None,
            )
            captured = conn.execute(
                'SELECT captured_at FROM tail_snapshots WHERE trade_date=? AND symbol=?',
                (args.date, item.symbol),
            ).fetchone()
            captured_at = datetime.fromisoformat(captured['captured_at']) if captured else None
            rows.append({
                'signal_id': item.signal_id,
                'symbol': item.symbol,
                'tail_price': tail_prices.get(item.symbol),
                'resistance_zone_high': sell.zone_high if sell else signal.zone_high,
                'minutes_since_tail': int((datetime.now(timezone.utc) - captured_at).total_seconds() / 60) if captured_at else None,
            })
        print(json.dumps({'unpaired_t_buy': rows, 'no_receipt': no_receipt}, ensure_ascii=False, indent=2))
    elif args.command == 'evening':
        print(json.dumps(run_evening(get_connection(), args.input), ensure_ascii=False, indent=2))
    elif args.command == 'next-routine':
        config_path = Path(__file__).parents[0] / 'config' / 'config.yaml'
        holidays = load_holidays(yaml.safe_load(config_path.read_text(encoding='utf-8')).get('holidays'))
        scheduled = next_routine(datetime.now().astimezone(), holidays)
        print(json.dumps({
            'routine': scheduled.routine.value,
            'run_at': scheduled.run_at.isoformat(timespec='seconds'),
        }, ensure_ascii=False, indent=2))
    elif args.command == 't-size':
        rows = get_connection().execute('SELECT * FROM signals WHERE id=?', (args.signal_id,)).fetchall()
        if not rows:
            raise SystemExit('signal not found')
        from .jobs.routines import signal_from_row

        updated = apply_t_size_choice(signal_from_row(rows[0]), TSizeChoice(args.choice))
        from .db.session import save_signal

        save_signal(get_connection(), updated)
        get_connection().commit()
        print(json.dumps({'signal_id': updated.id, 'size_hint': updated.size_hint}, ensure_ascii=False))
    elif args.command == 'report':
        market = json.loads(args.market_json.read_text(encoding='utf-8')) if args.market_json else {}
        text = build_report(args.date, market)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding='utf-8')
        else:
            print(text)
    elif args.command == 'market-input':
        payload = fetch_market_input(get_connection(), args.date, Path(__file__).parents[0] / 'config' / 'config.yaml')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({
            'output': str(args.output),
            'assignments': len(payload['assignments']),
            'rejected': payload['rejected'],
        }, ensure_ascii=False, indent=2))
    elif args.command == 'tail-snapshot':
        now = datetime.fromisoformat(args.now) if args.now else datetime.now().astimezone()
        result = capture_tail_snapshots(get_connection(), args.date, now)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'pool-performance':
        from .jobs.performance import pool_performance_report

        print(json.dumps(pool_performance_report(
            get_connection(), args.date, window=args.window,
        ), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
