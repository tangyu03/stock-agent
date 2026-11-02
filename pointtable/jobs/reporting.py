"""报告投影与计划日期：cli 的取数/装配层，从 cli.py 迁出（批次3）。

build_report 是唯一的报告投影入口，两种意图都走它：
- plan 视图：report_date=目标交易日，market=晚间输入 payload（含 assignments/
  signal_rows/rejected，候选与对账齐全）；
- 结算视图：report 命令，report_date=报告日，market 通常为空——已结算口径，
  无候选、无名称补充（名字只来自 legacy 持仓表）。

两种视图写同一路径族（reports/{date}-*.md）时内容不同，cli 对 report 命令
拒写 -plan.md 文件名以防误覆盖。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..core.calendar import is_trading_day
from ..db.session import (
    get_connection,
    load_correction_events,
    load_event_calendar,
    load_market_day,
    load_market_snapshots,
    load_pending_correction_anchors,
    load_report_signal_snapshot,
    load_stop_settlements,
    signal_from_row,
)
from ..stats import behavior, pool_quality, signal_quality, stop_outcomes
from .evening import load_risk_config, signal_day_trigger_review
from .settle import run_settle


def build_report(
    report_date: str,
    market: dict[str, Any] | None = None,
) -> str:
    from ..notify.render import render_report

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
    portfolio_path = Path(__file__).parents[2] / 'legacy' / 'config' / 'portfolio.yaml'
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
    risk_config = load_risk_config(Path(__file__).parents[2] / 'config' / 'config.yaml')
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
    # §0.1 报告头生产者：数据基准日结算行 + 报告快照齐备才算"已结算"；
    # 存在定性待补（ANOMALOUS）结算时不得只写"已结算"
    market_day = load_market_day(get_connection(), signal_date)
    settled = market_day is not None and report_snapshot is not None
    report_status = '已结算' if settled else '未结算'
    if settled:
        blocked = sum(
            1
            for row in market_for_report.get('stop_settlements') or []
            if str((row or {}).get('data_status') or '').upper() == 'ANOMALOUS'
            or not (row or {}).get('settlement_outcome')
        )
        if blocked:
            report_status += f'｜定性待补{blocked}'
    market_for_report['report_status'] = report_status
    if market_day is not None:
        market_for_report['circuit_day'] = bool(market_day['circuit_day'])
    else:
        # 结算行缺失时按当日量能分层/指数趋势现算（§5 circuit_breaker 规则）
        from ..core.volume import is_circuit_day

        circuit_config = risk_config.get('circuit_breaker') or {}
        market_for_report['circuit_day'] = is_circuit_day(
            str(market_for_report.get('tier') or (market or {}).get('tier', '')),
            str(market_for_report.get('index_trend') or (market or {}).get('index_trend', '')),
            volume_tier=str(circuit_config.get('volume_tier') or 'LOW'),
            index_trend_rule=str(circuit_config.get('index_trend') or 'DOWN'),
        )
    # §8 保护状态：缺失时报告也要显示结构化原因
    from .settle import pool_protection_states

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
    config = load_risk_config(Path(__file__).parents[2] / 'config' / 'config.yaml')
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
    risk_config = load_risk_config(Path(__file__).parents[2] / 'config' / 'config.yaml')
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
