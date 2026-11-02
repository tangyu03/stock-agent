from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .core.auction import AuctionPattern
from .core.calendar import is_trading_day, load_holidays
from .core.generator import TSizeChoice, apply_t_size_choice
from .db.session import (
    get_connection,
    init_db,
    set_database_path,
    signal_from_row,
)
from .domain.reconciliation import merge_rejections
from .jobs.auction_call import run_auction
from .jobs.cutover import expire_legacy_signals, formal_push_ready, switch_push_mode
from .jobs.evening import fetch_market_input, load_risk_config, run_evening
from .jobs.migration import legacy_signals, migration_status
from .jobs.pool_admin import (
    POOL_CONFIG_PATH,
    add_pool_entry,
    audit_pool_themes,
    list_holdings,
    list_pool,
    sync_pool_from_config,
)
from .jobs.reporting import build_report, plan_dates, settle_before_evening, stats_report
from .jobs.restore_check import capture_tail_snapshots, restore_check_report, run_restore
from .jobs.settle import run_settle
from .jobs.shadow import run_shadow
from .jobs.watch import run_watch
from .stats import pool_performance_report


def _configure_logging() -> None:
    logging.basicConfig(
        filename=Path(__file__).parents[1] / 'data' / 'pointtable.log',
        encoding='utf-8',
        level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
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
    pool_sync = commands.add_parser('pool-sync')
    pool_sync.add_argument('--date', default=None)
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


def _load_json_arg(path: Path | None) -> Any:
    '''可选 --xxx-json 参数统一读取（utf-8-sig 容忍 BOM）；缺省 None。'''
    return json.loads(path.read_text(encoding='utf-8-sig')) if path else None


def _settle_command_kwargs(
    args: argparse.Namespace,
    *,
    risk_config: dict,
    now: datetime,
    created_before: date | None = None,
    event_source: str = 'live_settle',
    run_id: str | None = None,
) -> dict[str, Any]:
    '''settle/replay 共用的 run_settle 入参装配：可选 JSON 文件 → 映射，配置兜底。'''
    zone_path = args.stop_zones_json or args.market_json
    stop_zones = _load_json_arg(zone_path)
    if stop_zones and args.market_json and not args.stop_zones_json:
        stop_zones = stop_zones.get('stop_zones')
    market_payload = _load_json_arg(args.market_json) or {}
    market_fields = market_payload.get('market') if isinstance(market_payload.get('market'), dict) else market_payload
    holidays = _load_json_arg(args.holidays_json)
    return dict(
        stop_zones=stop_zones,
        close_prices=_load_json_arg(args.close_prices_json),
        volume_ratios=_load_json_arg(args.volume_ratios_json),
        break_contexts=_load_json_arg(args.break_contexts_json),
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
        created_before=created_before,
        event_source=event_source,
        run_id=run_id,
        circuit_breaker=risk_config.get('circuit_breaker') or {},
    )


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
    elif args.command == 'pool-sync':
        print(json.dumps(
            sync_pool_from_config(
                args.date or date.today().isoformat(),
                config_path=POOL_CONFIG_PATH,
            ),
            ensure_ascii=False, indent=2,
        ))
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
        from .notify.push import PushPlusSender

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
            break_context=_load_json_arg(args.context_json),
            retest_volume_min=(
                args.retest_volume_min
                if args.retest_volume_min is not None
                else risk_config['retest_volume_min']
            ),
            prices=_load_json_arg(args.prices_json),
            context_prices=_load_json_arg(args.context_prices_json),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'settle':
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        risk_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        print(json.dumps(run_settle(
            get_connection(),
            now,
            **_settle_command_kwargs(args, risk_config=risk_config, now=now),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'replay':
        # §3.2 event_source=replay：以当时的输入复算收盘处理，run_id 标记本次重放
        replay_now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        replay_config = load_risk_config(Path(__file__).parents[1] / 'config' / 'config.yaml')
        run_id = f'replay-{args.date}-{replay_now.strftime("%Y%m%dT%H%M%S")}'
        print(json.dumps(run_settle(
            get_connection(),
            replay_now,
            **_settle_command_kwargs(
                args,
                risk_config=replay_config,
                now=replay_now,
                created_before=date.fromisoformat(args.date) + timedelta(days=1),
                event_source='replay',
                run_id=run_id,
            ),
        ), ensure_ascii=False, indent=2))
    elif args.command == 'restore':
        print(json.dumps(run_restore(get_connection(), args.close_price), ensure_ascii=False, indent=2))
    elif args.command == 'restore-check':
        print(json.dumps(restore_check_report(get_connection(), args.date), ensure_ascii=False, indent=2))
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
        market = _load_json_arg(args.market_json) or {}
        text = build_report(args.date, market)
        if args.output:
            # plan 视图（候选/对账齐全）只由 plan 命令产出；report 是结算视图，
            # 拒写 -plan.md 文件名，防止用未结算投影覆盖已生成的计划报告
            if args.output.name.endswith('-plan.md'):
                raise SystemExit(
                    f'refuse to overwrite a plan report with the settlement view: {args.output}; '
                    'use `plan` to regenerate plan reports, or pick another --output name'
                )
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
        # §8 对账闭合：生成阶段处置并入 payload['rejected']，回写输入快照供 report 命令复用
        generation_rejections = list(evening.get('generation_rejections') or [])
        if generation_rejections:
            payload['rejected'] = merge_rejections(payload.get('rejected'), generation_rejections)
            input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
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
