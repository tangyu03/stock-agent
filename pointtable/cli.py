from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path

import yaml

from .db.session import (
    get_connection,
    init_db,
    load_market_day,
    load_pending_correction_anchors,
    load_correction_events,
    load_event_calendar,
    load_market_snapshots,
    load_report_signal_snapshot,
    load_stop_settlements,
    signal_from_row,
    set_database_path,
)
from .jobs.cutover import expire_legacy_signals, formal_push_ready, switch_push_mode
from .core.generator import TSizeChoice, apply_t_size_choice
from .core.sector import load_theme_map
from .core.calendar import load_holidays
from .core.calendar import is_trading_day
from .core.auction import AuctionPattern
from .jobs.auction_call import run_auction
from .jobs.evening import evening_report, fetch_market_input, load_risk_config, run_evening, signal_day_trigger_review
from .jobs.restore_check import capture_tail_snapshots, load_tail_prices, run_restore
from .jobs.settle import run_settle
from .jobs.watch import run_watch
from .jobs.migration import migration_status
from .jobs.shadow import run_shadow
from .stats import pool_performance_report
from .jobs.evening import evening_report
from .stats import behavior, correction_hit_rate, pool_quality, signal_quality, stop_outcomes
from .notify.push import PushPlusSender


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _configure_logging() -> None:
    logging.basicConfig(
        filename=Path(__file__).parents[1] / 'data' / 'pointtable.log',
        encoding='utf-8',
        level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )


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


def stats_report(performance: dict | None = None, date: str | None = None) -> dict:
    conn = get_connection()
    signals = [dict(row) for row in conn.execute('SELECT * FROM signals')]
    stop_tracking = {
        str(row['signal_id']): row
        for row in (performance or {}).get('stop_tracking', [])
    }
    for row in signals:
        row.update(stop_tracking.get(str(row['id']), {}))
    pool_rows = [dict(row) for row in conn.execute('SELECT symbol, theme_label FROM pool GROUP BY symbol')]
    market_rows = load_market_snapshots(conn, date)
    behavior_data = behavior(signals)
    if performance:
        behavior_data['restore_violations'] = (performance.get('restore') or {}).get('violations')
    return {
        'signal_quality': signal_quality(signals, market_rows),
        'correction_hit_rate': performance.get('correction_hit_rate') if performance else None,
        'stop_outcomes': stop_outcomes(signals),
        'behavior': behavior_data,
        'pool_quality': pool_quality(pool_rows, (performance or {}).get('performance') or []),
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
        theme_map_path or Path(__file__).parents[1] / 'config' / 'theme_map.yaml'
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
    from .notify.render import render_report

    signal_date = str((market or {}).get('trade_date') or report_date)
    report_snapshot = load_report_signal_snapshot(get_connection(), report_date)
    signal_rows = report_snapshot or [
        signal_from_row(row)
        for row in get_connection().execute(
            '''
            SELECT * FROM signals
            WHERE created_at <= ? OR (closed_at IS NOT NULL AND closed_at >= ?)
            ORDER BY created_at, id
            ''',
            (report_date + 'T23:59:59', signal_date + 'T00:00:00'),
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
    market_for_report['signal_rows'] = list((market or {}).get('signal_rows') or [])
    market_for_report['rejected'] = list((market or {}).get('rejected') or [])
    market_for_report['name_map'] = {
        **{str(code): str(name) for code, name in ((market or {}).get('name_map') or {}).items()},
        **name_by_symbol,
    }
    market_for_report['spot_prices'] = {
        **{str(code): value for code, value in ((market or {}).get('spot_prices') or {}).items()},
    }
    for key in ('pool_total', 'watchlist_limit', 'retest_window_days'):
        if (market or {}).get(key) is not None:
            market_for_report[key] = market[key]
    risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
    market_for_report.setdefault('retest_window_days', risk_config['retest_window_days'])
    market_for_report.setdefault('holidays', [value.isoformat() for value in risk_config['holidays']])
    market_for_report['recent_stop_counts'] = (market or {}).get('recent_stop_counts') or {}
    market_for_report['signal_date'] = signal_date
    market_for_report['context_marks'] = [
        dict(row) for row in get_connection().execute(
            'SELECT * FROM context_marks WHERE date=? ORDER BY context_type, code',
        (signal_date,),
        ).fetchall()
    ]
    market_for_report['event_calendar'] = load_event_calendar(get_connection(), signal_date)
    market_for_report['correction_anchors'] = [
        anchor
        for anchor in load_pending_correction_anchors(get_connection())
        if anchor['trade_date'] == signal_date
    ]
    market_for_report['correction_events'] = load_correction_events(get_connection(), signal_date)
    market_for_report['stop_settlements'] = load_stop_settlements(
        get_connection(), signal_date, event_source='live_settle',
    )
    market_for_report['execution_review'] = signal_day_trigger_review(get_connection(), signal_date)
    # §0.1 报告头生产者：数据基准日结算行 + 报告快照齐备才算“已结算”
    market_day = load_market_day(get_connection(), signal_date)
    settled = market_day is not None and report_snapshot is not None
    market_for_report['report_status'] = '已结算' if settled else '未结算'
    if market_day is not None:
        market_for_report['circuit_day'] = bool(market_day['circuit_day'])
    else:
        # 结算行缺失时按当日量能分层/指数趋势现算（§5 circuit_breaker 规则）
        from .core.volume import is_circuit_day

        circuit_config = risk_config.get('circuit_breaker') or {}
        market_for_report['circuit_day'] = is_circuit_day(
            str(market_for_report.get('tier') or (market or {}).get('tier', '')),
            str(market_for_report.get('index_trend') or (market or {}).get('index_trend', '')),
            volume_tier=str(circuit_config.get('volume_tier') or 'LOW'),
            index_trend_rule=str(circuit_config.get('index_trend') or 'DOWN'),
        )
    # §8 保护状态：缺失时报告也要显示结构化原因
    from .jobs.settle import pool_protection_states

    market_for_report['protection_states'] = {
        symbol: [mode, reason]
        for symbol, (mode, reason) in pool_protection_states(get_connection()).items()
    }
    market_for_report['ev_negative_strategies'] = sorted({
        signal.strategy_id
        for signals in by_symbol.values()
        for signal in signals
        if signal.premise.get('ev_negative')
    })
    return render_report(report_date, market_for_report, [], signal_rows)


def _next_trading_day(value: date, holidays: set[date]) -> date:
    current = value + timedelta(days=1)
    while not is_trading_day(current, holidays):
        current += timedelta(days=1)
    return current


def _previous_trading_day(value: date, holidays: set[date]) -> date:
    current = value - timedelta(days=1)
    while not is_trading_day(current, holidays):
        current -= timedelta(days=1)
    return current


def plan_dates(target_date: str | None = None) -> dict[str, str]:
    config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
    holidays = config['holidays']
    if target_date:
        target = date.fromisoformat(target_date)
        if not is_trading_day(target, holidays):
            target = _next_trading_day(target, holidays)
    else:
        current = datetime.now().astimezone()
        if is_trading_day(current.date(), holidays) and current.time() < time(15, 5):
            target = current.date()
        else:
            target = _next_trading_day(current.date(), holidays)
    return {
        'target_date': target.isoformat(),
        'signal_date': _previous_trading_day(target, holidays).isoformat(),
    }


def settle_before_evening(signal_date: str, payload: dict) -> dict[str, list[str]]:
    now = datetime.now().astimezone()
    signal_day = date.fromisoformat(signal_date)
    if signal_day > now.date() or (
        signal_day == now.date() and now.time() < time(15, 5)
    ):
        return {}
    now = datetime.combine(
        signal_day,
        time(15, 5),
        tzinfo=now.tzinfo,
    )
    risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
    signal_symbols = {
        str(row['symbol'])
        for row in (payload.get('signal_rows') or [])
    } | {
        str(row['symbol'])
        for row in (payload.get('assignments') or [])
    }
    close_prices = {
        str(row['symbol']): float(row['est_close'])
        for row in (payload.get('signal_rows') or [])
        if row.get('symbol') in signal_symbols and row.get('est_close') is not None
    }
    low_prices = {
        str(row['symbol']): float(row['low'])
        for row in (payload.get('signal_rows') or [])
        if row.get('symbol') in signal_symbols and row.get('low') is not None
    }
    high_prices = {
        str(row['symbol']): float(row['high'])
        for row in (payload.get('signal_rows') or [])
        if row.get('symbol') in signal_symbols and row.get('high') is not None
    }
    volume_ratios = {
        str(row['symbol']): float(row['volume_ratio'])
        for row in (payload.get('signal_rows') or [])
        if row.get('volume_ratio') is not None
    }
    break_contexts = {
        str(row['symbol']): {
            'volume_ratio': row.get('volume_ratio') or 0,
            'active_delta': row.get('active_delta'),
            'stock_triggered': False,
            'sector_triggered': False,
            'index_triggered': False,
        }
        for row in payload.get('signal_rows') or []
    }
    return run_settle(
        get_connection(),
        now,
        stop_zones=payload.get('stop_zones') or {},
        close_prices=close_prices,
        low_prices=low_prices,
        high_prices=high_prices,
        volume_ratios=volume_ratios,
        break_contexts=break_contexts,
        market_tier=str((payload.get('market') or {}).get('tier') or 'NORMAL'),
        index_trend=str((payload.get('market') or {}).get('index_trend') or 'UP'),
        created_before=signal_day,
        retest_window_days=risk_config['retest_window_days'],
        retest_volume_min=risk_config['retest_volume_min'],
        holidays=risk_config['holidays'],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pointtable')
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--legacy', action='store_true', help='query system migration audit rows')
    commands = parser.add_subparsers(dest='command')
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
    replay = commands.add_parser('replay', help='§3.2 重放：event_source=replay 复算指定信号日收盘')
    replay.add_argument('--date', required=True, help='信号日（created_before 前产生的信号参与复算）')
    replay.add_argument('--now')
    replay.add_argument('--stop-zones-json', type=Path)
    replay.add_argument('--close-prices-json', type=Path)
    replay.add_argument('--market-json', type=Path)
    replay.add_argument('--volume-ratios-json', type=Path)
    replay.add_argument('--break-contexts-json', type=Path)
    replay.add_argument('--retest-window-days', type=int)
    replay.add_argument('--retest-volume-min', type=float)
    replay.add_argument('--holidays-json', type=Path)
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
    plan = commands.add_parser('plan')
    plan.add_argument('--date')
    performance = commands.add_parser('pool-performance')
    performance.add_argument('--date', required=True)
    performance.add_argument('--window', type=int, default=5)
    commands.add_parser('legacy')
    commands.add_parser('pool-audit')
    commands.add_parser('migration-status')
    return parser


class Routine(str, Enum):
    AUCTION = 'auction'
    WATCH = 'watch'
    TAIL = 'tail'
    RESTORE = 'restore'
    SETTLE = 'settle'
    EVENING = 'evening'


FIXED_TIMES = {
    Routine.AUCTION: time(9, 25),
    Routine.WATCH: time(9, 30),
    Routine.TAIL: time(14, 30),
    Routine.RESTORE: time(14, 31),
    Routine.SETTLE: time(15, 5),
    Routine.EVENING: time(15, 30),
}


@dataclass(frozen=True)
class ScheduledRoutine:
    routine: Routine
    run_at: datetime


def next_routine(now: datetime, holidays: set | None = None) -> ScheduledRoutine:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    candidate_date = now.date()
    while True:
        if is_trading_day(candidate_date, holidays):
            for routine in Routine:
                scheduled = datetime.combine(
                    candidate_date, FIXED_TIMES[routine], tzinfo=now.tzinfo
                )
                if scheduled > now:
                    return ScheduledRoutine(routine, scheduled)
        candidate_date += timedelta(days=1)
def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    if not args.command and not args.legacy:
        raise SystemExit('pointtable: error: a command is required')
    if args.db is not None:
        set_database_path(args.db)
    init_db()
    if args.legacy:
        print(json.dumps([dict(row) for row in legacy_signals()], ensure_ascii=False, indent=2))
        return 0
    if args.command == 'stats':
        performance = None
        if args.date:
            performance = pool_performance_report(get_connection(), args.date, window=args.window)
        print(json.dumps(stats_report(performance, args.date), ensure_ascii=False, indent=2))
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
                json.loads(args.context_json.read_text(encoding='utf-8-sig'))
                if args.context_json else None
            ),
            retest_volume_min=(
                args.retest_volume_min
                if args.retest_volume_min is not None
                else risk_config['retest_volume_min']
            ),
            prices=(
                json.loads(args.prices_json.read_text(encoding='utf-8-sig'))
                if args.prices_json else None
            ),
            context_prices=(
                json.loads(args.context_prices_json.read_text(encoding='utf-8-sig'))
                if args.context_prices_json else None
            ),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'settle':
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        zone_path = args.stop_zones_json or args.market_json
        stop_zones = json.loads(zone_path.read_text(encoding='utf-8-sig')) if zone_path else None
        if stop_zones and args.market_json and not args.stop_zones_json:
            stop_zones = stop_zones.get('stop_zones')
        close_prices = json.loads(args.close_prices_json.read_text(encoding='utf-8-sig')) if args.close_prices_json else None
        volume_ratios = json.loads(args.volume_ratios_json.read_text(encoding='utf-8-sig')) if args.volume_ratios_json else None
        break_contexts = json.loads(args.break_contexts_json.read_text(encoding='utf-8-sig')) if args.break_contexts_json else None
        market_payload = json.loads(args.market_json.read_text(encoding='utf-8-sig')) if args.market_json else {}
        market_fields = market_payload.get('market') if isinstance(market_payload.get('market'), dict) else market_payload
        holidays = json.loads(args.holidays_json.read_text(encoding='utf-8-sig')) if args.holidays_json else None
        risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        print(json.dumps(run_settle(
            get_connection(),
            now,
            stop_zones=stop_zones,
            close_prices=close_prices,
            volume_ratios=volume_ratios,
            break_contexts=break_contexts,
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
            circuit_breaker=risk_config.get('circuit_breaker') or {},
        ), ensure_ascii=False, indent=2))
    elif args.command == 'replay':
        # §3.2 event_source=replay：以当时的输入复算收盘处理，run_id 标记本次重放
        replay_now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        zone_path = args.stop_zones_json or args.market_json
        replay_stop_zones = json.loads(zone_path.read_text(encoding='utf-8-sig')) if zone_path else None
        if replay_stop_zones and args.market_json and not args.stop_zones_json:
            replay_stop_zones = replay_stop_zones.get('stop_zones')
        replay_market = json.loads(args.market_json.read_text(encoding='utf-8-sig')) if args.market_json else {}
        replay_fields = replay_market.get('market') if isinstance(replay_market.get('market'), dict) else replay_market
        replay_holidays = json.loads(args.holidays_json.read_text(encoding='utf-8-sig')) if args.holidays_json else None
        replay_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        run_id = f'replay-{args.date}-{replay_now.strftime("%Y%m%dT%H%M%S")}'
        print(json.dumps(run_settle(
            get_connection(),
            replay_now,
            stop_zones=replay_stop_zones,
            close_prices=(
                json.loads(args.close_prices_json.read_text(encoding='utf-8-sig'))
                if args.close_prices_json else None
            ),
            volume_ratios=(
                json.loads(args.volume_ratios_json.read_text(encoding='utf-8-sig'))
                if args.volume_ratios_json else None
            ),
            break_contexts=(
                json.loads(args.break_contexts_json.read_text(encoding='utf-8-sig'))
                if args.break_contexts_json else None
            ),
            retest_window_days=(
                args.retest_window_days
                if args.retest_window_days is not None
                else replay_config['retest_window_days']
            ),
            retest_volume_min=(
                args.retest_volume_min
                if args.retest_volume_min is not None
                else replay_config['retest_volume_min']
            ),
            holidays=set(replay_holidays) if replay_holidays is not None else replay_config['holidays'],
            market_tier=str(replay_fields.get('tier') or 'NORMAL'),
            index_trend=str(replay_fields.get('index_trend') or 'UP'),
            created_before=date.fromisoformat(args.date) + timedelta(days=1),
            event_source='replay',
            run_id=run_id,
            circuit_breaker=replay_config.get('circuit_breaker') or {},
        ), ensure_ascii=False, indent=2))
    elif args.command == 'restore':
        print(json.dumps(run_restore(get_connection(), args.close_price), ensure_ascii=False, indent=2))
    elif args.command == 'restore-check':
        from .jobs.restore_check import cascade_violations, find_unpaired_t_buy

        conn = get_connection()
        t_signals = [signal_from_row(row) for row in conn.execute("SELECT * FROM signals WHERE tag='t'").fetchall()]
        unpaired, _ = find_unpaired_t_buy(t_signals, trade_date=args.date, active_only=True)
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
        print(json.dumps({
            'unpaired_t_buy': rows,
            'cascade_violations': cascade_violations(t_signals),
        }, ensure_ascii=False, indent=2))
    elif args.command == 'evening':
        print(json.dumps(run_evening(get_connection(), args.input), ensure_ascii=False, indent=2))
    elif args.command == 'next-routine':
        config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
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
        updated = apply_t_size_choice(signal_from_row(rows[0]), TSizeChoice(args.choice))
        from .db.session import save_signal

        save_signal(get_connection(), updated)
        get_connection().commit()
        print(json.dumps({'signal_id': updated.id, 'size_hint': updated.size_hint}, ensure_ascii=False))
    elif args.command == 'report':
        market = json.loads(args.market_json.read_text(encoding='utf-8-sig')) if args.market_json else {}
        text = build_report(args.date, market)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding='utf-8')
        else:
            print(text)
    elif args.command == 'plan':
        dates = plan_dates(args.date)
        config_path = Path(__file__).parents[1] / 'config' / 'config.yaml'
        input_path = Path('pointtable') / 'data' / ('evening-' + dates['target_date'] + '.json')
        payload = fetch_market_input(get_connection(), dates['signal_date'], config_path)
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        settled = settle_before_evening(dates['signal_date'], payload)
        evening = run_evening(get_connection(), input_path, dates['target_date'])
        current = datetime.now().astimezone()
        watch_result: dict[str, Any] = {}
        if dates['target_date'] == current.date().isoformat() and current.time() < time(15, 5):
            watch_result = run_watch(
                get_connection(),
                0.0,
                False,
                current,
                prices={
                    str(row['symbol']): float(row['est_close'])
                    for row in payload.get('signal_rows') or []
                    if row.get('est_close') is not None
                },
            )
        report_path = Path('reports') / (dates['target_date'] + '-plan.md')
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            build_report(dates['target_date'], payload),
            encoding='utf-8',
        )
        print(json.dumps({
            'report': str(report_path),
            'target_date': dates['target_date'],
            'signal_date': dates['signal_date'],
            'input': str(input_path),
            'settle': settled,
            'watch': watch_result,
            'signal_count': len(evening['signal_ids']),
        }, ensure_ascii=False, indent=2))
    elif args.command == 'market-input':
        payload = fetch_market_input(get_connection(), args.date, Path(__file__).parents[1] / 'config' / 'config.yaml')
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
        print(json.dumps(pool_performance_report(
            get_connection(), args.date, window=args.window,
        ), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
