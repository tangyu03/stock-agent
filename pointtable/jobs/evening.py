from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
import yaml

from ..core.calendar import is_trading_day, load_holidays, next_trading_day
from ..core.generator import (
    Strategy,
    apply_size_hints,
    build_pool_plans,
    _signal,
    generate_holding_rows,
    generate_entry,
    premise_ok,
)
from ..core.sector import load_theme_map
from ..core.selector import COOLDOWN_STOP_COUNT, MA_FAMILY, cooled_out
from ..core.trend import classify_trend, moving_average
from ..domain.reconciliation import record_rejection
# re-export：迁移到 domain/rules.py 的纯函数仍从本模块出口（测试/调用方的兼容面）
from ..domain.rules import (  # noqa: F401
    SZ_AMOUNT_INDEX,
    VOLUME_TIER_HIGH_MIN,
    VOLUME_TIER_LOW_MAX,
    CandidateContext,
    _amount_yi,
    _closes,
    _latest,
    _value,
    _volume_ratio,
    _zone,
    evaluate_candidates,
    fill_missing_spot_rows,
    lhb_decision,
    market_tier,
    truncate_watchlist,
    volume_breakout_decision,
)
from ..db.session import (
    load_market_snapshots, load_signals, save_context_marks, save_event_calendar,
    save_journal, save_market_snapshots, save_report_signal_snapshot, save_signals,
)
from ..models.context_mark import ContextMark, ContextType
from ..models.event_calendar import EventCalendar
from ..models.journal import JournalEntry
from ..models.signal import Signal, SignalRole, SignalStatus, SignalTag, SignalTrend, SignalValidPolicy
from ..notify.render import render_report
from ..stats import attach_hypothetical_pnl, signal_quality
from .restore_check import load_tail_prices
from .settle import pool_protection_states


def build_context_marks(
    index_rows: list[Mapping[str, Any]],
    sector_rows: list[Mapping[str, Any]],
    trade_date: date,
    created_at: datetime,
) -> list[ContextMark]:
    marks: list[ContextMark] = []
    for context_type, rows in (
        (ContextType.INDEX, index_rows),
        (ContextType.SECTOR, sector_rows),
    ):
        for row in rows:
            symbol = str(row.get('symbol') or '')
            zone = row.get('zone')
            if not symbol or not zone or float(zone[0]) >= float(zone[1]):
                continue
            marks.append(ContextMark(
                date=trade_date,
                context_type=context_type,
                code=symbol,
                level_low=float(zone[0]),
                level_high=float(zone[1]),
                label=str(row.get('label') or ''),
                data_status=str(row.get('data_status') or 'OK'),
                triggered_at=None,
                created_at=created_at,
            ))
    return marks


def build_evening_signals(
    strategies: Mapping[str, Strategy],
    pool_plans: list[Mapping[str, Any]],
    market_tier_name: str,
    created_at: datetime,
    existing_signals: list[Signal] | None = None,
    recent_stop_counts: Mapping[str, Mapping[str, int]] | None = None,
    same_ma_cooldown_days: int = 2,
    skip_zw_symbols: set[str] | None = None,
    rejections: list[dict[str, str]] | None = None,
) -> list[Signal]:
    rows: list[Signal] = []
    active_entries = {
        (signal.symbol, signal.strategy_id)
        for signal in (existing_signals or [])
        if signal.signal_role is SignalRole.ENTRY
        and signal.status in {SignalStatus.ACTIVE, SignalStatus.TRIGGERED}
    }
    trade_date = created_at.date().isoformat()

    def _reject(symbol: str, reason: str) -> None:
        # §8 对账闭合：生成阶段的每一次丢弃都必须留下结构化处置记录
        record_rejection(rejections, symbol, reason)

    for plan in pool_plans:
        strategy = strategies.get(str(plan.get('strategy_id') or ''))
        symbol = str(plan['symbol'])
        if strategy is None:
            _reject(symbol, 'strategy_config_missing')
            continue
        if not premise_ok(plan.get('premise_facts') or {}):
            _reject(symbol, 'premise_failed')
            continue
        if (symbol, strategy.id) in active_entries:
            _reject(symbol, 'duplicate_active_entry')
            continue
        if cooled_out(
            [strategy.id],
            (recent_stop_counts or {}).get(symbol, {}),
            same_ma_cooldown_days,
        ) == []:
            _reject(symbol, 'ma_family_cooldown')
            continue
        rows.extend(generate_entry(
            strategy,
            symbol,
            (float(plan['entry_low']), float(plan['entry_high'])),
            created_at,
            parent_id=plan.get('parent_id'),
        ))
        # §4 generator：entry 生成时确保同 symbol 最多一条 ACTIVE stop
        # 已有则重锚 parent；没有则按 Z/W 同生新建（池内路径覆盖的票除外）
        if symbol in (skip_zw_symbols or set()):
            continue
        active_stop = next(
            (
                signal for signal in (existing_signals or [])
                if signal.symbol == symbol
                and signal.tag is SignalTag.STOP
                and signal.status is SignalStatus.ACTIVE
            ),
            None,
        )
        parent_anchor = str(plan.get('parent_id') or f'pool:{symbol}')
        if active_stop is not None:
            if active_stop.parent_id != parent_anchor:
                rows.append(active_stop.model_copy(update={'parent_id': parent_anchor}))
            continue
        if plan.get('stop_low') is None or plan.get('stop_high') is None:
            continue
        rows.append(_signal(
            f'{trade_date}-{symbol}-{strategy.id}-stop', strategy.id, symbol,
            'SELL', SignalTag.STOP,
            (float(plan['stop_low']), float(plan['stop_high'])),
            SignalTrend.UP, created_at, parent_anchor,
            SignalValidPolicy.UNTIL_STOPPED,
        ))
        if plan.get('profit_low') is not None and plan.get('profit_high') is not None:
            rows.append(_signal(
                f'{trade_date}-{symbol}-{strategy.id}-profit', strategy.id, symbol,
                'SELL', SignalTag.PROFIT,
                (float(plan['profit_low']), float(plan['profit_high'])),
                SignalTrend.UP, created_at, parent_anchor,
                SignalValidPolicy.UNTIL_STOPPED,
            ))
    return apply_size_hints(rows, market_tier_name)


def expire_cooled_candidates(
    existing_signals: list[Signal],
    recent_stop_counts: Mapping[str, Mapping[str, int]],
    created_at: datetime,
) -> list[Signal]:
    rows: list[Signal] = []
    for signal in existing_signals:
        family = MA_FAMILY.get(signal.strategy_id, signal.strategy_id)
        hits = int((recent_stop_counts.get(signal.symbol) or {}).get(family) or 0)
        if (
            signal.signal_role is SignalRole.ENTRY
            and signal.status is SignalStatus.ACTIVE
            and hits >= COOLDOWN_STOP_COUNT
        ):
            rows.append(signal.model_copy(update={
                'status': SignalStatus.EXPIRED,
                'status_reason': 'ma_family_cooldown',
                'closed_at': created_at,
            }))
    return rows


def build_event_calendar(
    strategies: Mapping[str, Strategy],
    pool_plans: list[Mapping[str, Any]],
    trade_date: date,
    created_at: datetime,
) -> list[EventCalendar]:
    events: dict[str, EventCalendar] = {}
    for plan in pool_plans:
        strategy = strategies.get(str(plan.get('strategy_id') or ''))
        symbol = str(plan.get('symbol') or '')
        if strategy is None or not symbol or str(strategy.y.get('when') or '') != 'tail_snapshot':
            continue
        label = str(strategy.y.get('label') or f'{strategy.id}:尾盘触发')
        events[symbol] = EventCalendar(
            date=trade_date,
            code=symbol,
            label=label,
            due_time=time(14, 30),
            payload={'strategy_id': strategy.id},
            created_at=created_at,
        )
    return list(events.values())


def build_holding_rows(
    holdings: list[Mapping[str, Any]],
    created_at: datetime,
) -> list[Signal]:
    rows: list[Signal] = []
    for holding in holdings:
        symbol = str(holding.get('symbol') or '')
        parent_id = holding.get('parent_id')
        stop_zone = holding.get('stop_zone')
        support_zone = holding.get('support_zone')
        resistance_zone = holding.get('resistance_zone')
        if not symbol or not parent_id or not stop_zone or not support_zone or not resistance_zone:
            raise ValueError('holding rows require symbol, parent_id and three zones')
        trend = SignalTrend.UP if str(holding.get('trend') or 'UP').upper() == 'UP' else SignalTrend.DOWN
        generated = generate_holding_rows(
            symbol,
            trend,
            (float(stop_zone[0]), float(stop_zone[1])),
            (float(support_zone[0]), float(support_zone[1])),
            (float(resistance_zone[0]), float(resistance_zone[1])),
            str(parent_id),
            created_at,
            'trailing' if holding.get('trailing') else 'fixed',
        )
        if holding.get('volatility_exceeded'):
            generated = [row for row in generated if row.tag is not SignalTag.T]
        rows.extend(generated)
    return rows


def build_pool_signals(
    rows: list[Mapping[str, Any]],
    created_at: datetime,
    existing_signals: list[Signal] | None = None,
    suppress_t_symbols: set[str] | None = None,
) -> list[Signal]:
    result: list[Signal] = []
    existing = list(existing_signals or [])
    trade_date = created_at.date().isoformat()

    for row in rows:
        symbol = str(row.get('symbol') or '')
        strategy_id = str(row.get('strategy_id') or 'pool')
        parent_id = str(row.get('parent_id') or '')
        trend = (
            SignalTrend.UP
            if str(row.get('trend') or 'UP').upper() == 'UP'
            else SignalTrend.DOWN
        )
        if not symbol or not parent_id:
            raise ValueError('pool signals require symbol and parent_id')
        prior_stops = [
            signal for signal in existing
            if signal.symbol == symbol
            and signal.tag is SignalTag.STOP
            and signal.status is SignalStatus.TRIGGERED
        ]
        prior_stop = max(prior_stops, key=lambda signal: signal.closed_at or signal.created_at, default=None)
        for signal in [
            item for item in existing
            if item.symbol == symbol
            and item.parent_id == parent_id
            and item.tag is SignalTag.T
            and item.status is SignalStatus.ACTIVE
        ]:
            result.append(signal.model_copy(update={
                'status': SignalStatus.EXPIRED,
                'status_reason': 'nightly_reset',
                'closed_at': created_at,
            }))

        desired = [
            _signal(
                f'{trade_date}-{symbol}-stop', strategy_id, symbol,
                'SELL', SignalTag.STOP,
                (float(row['stop_zone'][0]), float(row['stop_zone'][1])),
                trend, created_at, parent_id, SignalValidPolicy.UNTIL_STOPPED,
            ),
            _signal(
                f'{trade_date}-{symbol}-profit', strategy_id, symbol,
                'SELL', SignalTag.PROFIT,
                (float(row['profit_zone'][0]), float(row['profit_zone'][1])),
                trend, created_at, parent_id, SignalValidPolicy.UNTIL_STOPPED,
            ),
        ]
        if prior_stop is not None:
            desired = [
                signal.model_copy(update={
                    'premise': {
                        **signal.premise,
                        'prior_stop_id': prior_stop.id,
                    },
                })
                for signal in desired
            ]
        t_buy_zone = (
            float(row['t_buy_zone'][0]), float(row['t_buy_zone'][1]),
        )
        t_sell_zone = (
            float(row['t_sell_zone'][0]), float(row['t_sell_zone'][1]),
        )
        include_t = trend is SignalTrend.UP
        overlap_stop = False
        t_suppressed = symbol in (suppress_t_symbols or set())
        if include_t and t_suppressed:
            include_t = False
        if include_t:
            active_stops = [
                item for item in existing
                if item.symbol == symbol
                and item.tag is SignalTag.STOP
                and item.status is SignalStatus.ACTIVE
            ]
            overlap_stop = any(
                max(item.zone_low, t_buy_zone[0])
                <= min(item.zone_high, t_buy_zone[1])
                for item in [desired[0], *active_stops]
            )
            include_t = not overlap_stop
        if include_t and (
            t_buy_zone[0] <= 0 or t_buy_zone[0] >= t_buy_zone[1]
            or (t_buy_zone[1] - t_buy_zone[0]) / t_buy_zone[0] > 0.15
            or t_sell_zone[0] <= t_buy_zone[1]
        ):
            include_t = False
        if include_t:
            desired.extend([
                _signal(
                    f'{trade_date}-{symbol}-t-buy', strategy_id, symbol,
                    'BUY', SignalTag.T,
                    t_buy_zone,
                    trend, created_at, parent_id, SignalValidPolicy.INTRADAY,
                ),
                _signal(
                    f'{trade_date}-{symbol}-t-sell', strategy_id, symbol,
                    'SELL', SignalTag.T,
                    t_sell_zone,
                    trend, created_at, parent_id, SignalValidPolicy.INTRADAY,
                ),
            ])

        if not include_t and trend is SignalTrend.UP:
            if t_suppressed:
                reason = 'volatility_exceeded'
            elif overlap_stop:
                reason = 't_buy_overlaps_stop'
            else:
                reason = 'invalid_t_zone'
            desired = [
                signal.model_copy(update={
                    'premise': {**signal.premise, 't_suppression_reason': reason},
                })
                for signal in desired
            ]

        for signal in desired:
            candidates = [
                item for item in existing
                if item.symbol == signal.symbol
                and item.tag is signal.tag
                and item.side is signal.side
                and item.valid_policy is signal.valid_policy
                and item.status is SignalStatus.ACTIVE
            ]
            # T 每晚新建、不参与重锚（§3.2：T 不记 zone_update，滚动词只对 stop/profit）
            if candidates and signal.signal_role is not SignalRole.T:
                latest = max(candidates, key=lambda item: item.created_at)
                result.append(latest.model_copy(update={
                    'strategy_id': signal.strategy_id,
                    'parent_id': signal.parent_id,
                    'zone_low': signal.zone_low,
                    'zone_high': signal.zone_high,
                    'trend': signal.trend,
                }))
            else:
                result.append(signal)

        if trend is SignalTrend.DOWN:
            for signal in existing:
                if (
                    signal.symbol == symbol
                    and signal.parent_id == parent_id
                    and signal.tag is SignalTag.T
                    and signal.status is SignalStatus.ACTIVE
                ):
                    result.append(signal.model_copy(update={
                        'status': SignalStatus.EXPIRED,
                        'status_reason': 'trend_flip',
                        'closed_at': created_at,
                    }))
    return result


def evening_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
) -> str:
    context = dict(market)
    context.setdefault('missed_stop_hit', any(position.get('missed_stop_date') for position in positions))
    return render_report(report_date, context, positions)


logger = logging.getLogger(__name__)


def latest_entry_parent_ids(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, str | None]:
    parents: dict[str, str | None] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT id FROM signals WHERE symbol=? AND tag='enter' AND status='TRIGGERED' "
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (symbol,),
        ).fetchone()
        parents[symbol] = str(row['id']) if row else f'pool:{symbol}'
    return parents


def active_retest_symbols(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row['symbol'])
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM signals WHERE tag='retest_confirm' AND status='ACTIVE'"
        ).fetchall()
    }


def recent_stop_counts(
    conn: sqlite3.Connection,
    trade_date: str,
    window_days: int = 2,
    holidays: set[date] | None = None,
) -> dict[str, dict[str, int]]:
    if window_days < 0:
        raise ValueError('window_days must not be negative')
    current = date.fromisoformat(trade_date)
    counted = 0
    while counted < max(0, window_days - 1):
        current -= timedelta(days=1)
        if is_trading_day(current, holidays):
            counted += 1
    threshold = current.isoformat()
    rows = conn.execute(
        '''
        SELECT s.symbol, s.strategy_id AS strategy_id, COUNT(DISTINCT s.id) AS hits
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        WHERE s.tag='stop' AND s.status='TRIGGERED'
          AND j.to_status='TRIGGERED' AND j.created_at >= ?
        GROUP BY s.symbol, s.strategy_id
        ''',
        (threshold + 'T00:00:00',),
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        symbol = str(row['symbol'])
        strategy_id = str(row['strategy_id'] or 'unknown')
        family = MA_FAMILY.get(strategy_id, strategy_id)
        counts.setdefault(symbol, {})[family] = int(row['hits'])
    return counts


def build_market_input(
    pool_rows: Sequence[Mapping[str, Any]],
    index_rows: Sequence[Mapping[str, Any]],
    lhb: Sequence[Mapping[str, Any]],
    stock_klines: Mapping[str, Sequence[Mapping[str, Any]]],
    sector_klines: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    regulator_list: Sequence[str] = (),
    lhb_dumpers: Sequence[str] = (),
    tail_prices: Mapping[str, float] | None = None,
    entry_parent_ids: Mapping[str, str | None] | None = None,
    active_retest: set[str] | None = None,
    recent_stops: Mapping[str, Mapping[str, int]] | None = None,
    same_ma_cooldown_days: int = 2,
    lhb_checked: bool = True,
    two_source_deviation_max: float = 0.005,
    quote_sanity_enabled: bool = True,
    deep_pullback_ratio: float = 0.95,
    watchlist_limit: int | None = None,
    priority_order: Sequence[str] = ('holding', 'index', 'sector', 'candidate'),
    now: date | None = None,
    holidays: set[date] | None = None,
    name_map: Mapping[str, str] | None = None,
    active_deltas: Mapping[str, float] | None = None,
    sz_index_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    theme_map = load_theme_map(Path(__file__).parents[2] / 'config' / 'theme_map.yaml')
    closes = _closes(index_rows)
    if len(closes) < 20:
        logger.warning(
            'index closes insufficient: rows=%s first=%s last=%s',
            len(index_rows),
            index_rows[0] if index_rows else None,
            index_rows[-1] if index_rows else None,
        )
        raise ValueError('index klines must contain at least 20 closes')
    latest = _latest(index_rows) or {}
    latest_index_date = str(_value(latest, 'trade_date', '日期', 'date') or '')[:10]
    if now and latest_index_date != str(now):
        raise ValueError(
            f'index kline freshness mismatch: signal_date={now}, index_date={latest_index_date}'
        )
    amount_yi = _amount_yi(_value(latest, 'amount', '成交额'))
    if amount_yi is None:
        raise ValueError('index amount is missing')
    sh_amount_yi = amount_yi
    sz_amount_yi: float | None = None
    if sz_index_rows is not None:
        sz_amount_yi = _amount_yi(_value(_latest(sz_index_rows) or {}, 'amount', '成交额'))
        if sz_amount_yi is None:
            raise ValueError('sz index amount is missing')
        amount_yi = round(sh_amount_yi + sz_amount_yi, 2)
    amount_scope = 'sh+sz' if sz_amount_yi is not None else 'sh_only'
    sh_close = closes[-1]
    sh_ma5 = moving_average(closes, 5)
    index_trend = classify_trend(closes).value
    market_tier = (
        'LOW' if amount_yi < VOLUME_TIER_LOW_MAX
        else 'HIGH' if amount_yi >= VOLUME_TIER_HIGH_MIN
        else 'NORMAL'
    )

    for theme, rows in sector_klines.items():
        latest_sector_date = str(
            _value(_latest(rows) or {}, 'trade_date', '日期', 'date') or ''
        )[:10]
        if now and latest_sector_date > str(now):
            raise ValueError(
                f'sector kline is in the future: theme={theme}, sector_date={latest_sector_date}'
            )

    index_zone = _zone(index_rows, 5)
    index_context = [{'symbol': 'sh000001', 'zone': index_zone}] if index_zone else []
    sector_context: list[dict[str, Any]] = []
    themes = sorted({str(row.get('theme_label') or '') for row in pool_rows} - {'', 'unknown'})
    for theme in themes:
        zone = _zone(sector_klines.get(theme, []), 10)
        if zone:
            sector_context.append({'symbol': theme, 'zone': zone})

    if watchlist_limit is not None and watchlist_limit < len(pool_rows):
        pool_rows = truncate_watchlist(pool_rows, watchlist_limit, priority_order)
    evaluation = evaluate_candidates(pool_rows, stock_klines, CandidateContext(
        now=now,
        theme_map=theme_map,
        regulator_list=regulator_list,
        lhb=lhb,
        lhb_dumpers=lhb_dumpers,
        lhb_checked=lhb_checked,
        tail_prices=tail_prices or {},
        entry_parent_ids=entry_parent_ids or {},
        active_retest=active_retest or set(),
        recent_stops=recent_stops or {},
        same_ma_cooldown_days=same_ma_cooldown_days,
        two_source_deviation_max=two_source_deviation_max,
        quote_sanity_enabled=quote_sanity_enabled,
        deep_pullback_ratio=deep_pullback_ratio,
        holidays=holidays or set(),
        active_deltas=active_deltas or {},
    ))
    assignments = evaluation['assignments']
    rejected = evaluation['rejected']
    stop_zones = evaluation['stop_zones']
    pool_signal_rows = evaluation['pool_signal_rows']
    pool_total = evaluation['pool_total']
    return {
        'trade_date': str(now) if now else str(_value(_latest(index_rows) or {}, 'trade_date', '日期', 'date') or ''),
        'market_tier': market_tier,
        'market': {
            'amount_yi': amount_yi,
            'amount_scope': amount_scope,
            'sh_amount_yi': sh_amount_yi,
            'sz_amount_yi': sz_amount_yi,
            'tier': market_tier,
            'sh_close': sh_close,
            'sh_ma5': sh_ma5,
            'index_trend': index_trend,
        },
        'index_context': index_context,
        'sector_context': sector_context,
        'stop_zones': stop_zones,
        'assignments': assignments,
        'rejected': rejected,
        'lhb_checked': lhb_checked,
        'pool_total': pool_total,
        'watchlist_limit': watchlist_limit,
        'same_ma_cooldown_days': int(same_ma_cooldown_days),
        'recent_stop_counts': dict(recent_stops or {}),
        'holidays': sorted(holiday.isoformat() for holiday in (holidays or set())),
        'name_map': dict(name_map or {}),
        'signal_rows': pool_signal_rows,
    }


def load_risk_config(config_path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding='utf-8')) or {}
    return {
        'regulator_list': raw.get('regulator_list') or [],
        'lhb_dumpers': raw.get('lhb_dumpers') or [],
        'watchlist_limit': raw.get('watchlist_max', raw.get('watchlist_limit')),
        'circuit_breaker': raw.get('circuit_breaker') or {},
        'priority': raw.get('priority') or ['holding', 'index', 'sector', 'candidate'],
        'context_indices': (raw.get('market_context') or {}).get('indices') or ['sh000001'],
        'same_ma_cooldown_days': int(raw.get('reentry_rules', {}).get('same_ma_cooldown_days', 2)),
        'stats': raw.get('stats') or {},
        'guard': raw.get('guard') or {},
        'retest_window_days': int(raw.get('selector', {}).get('retest_window_days', 3)),
        'retest_volume_min': float(raw.get('selector', {}).get('retest_volume_min', 1.2)),
        'holidays': load_holidays(raw.get('holidays')),
    }


def fetch_market_input(
    conn: sqlite3.Connection,
    trade_date: str,
    config_path: str | Path,
) -> dict[str, Any]:

    from ..data import akshare

    pool_rows = [
        dict(row) for row in conn.execute(
        '''
        SELECT symbol, theme_label FROM pool
        WHERE date = (SELECT MAX(date) FROM pool WHERE date <= ?)
        ORDER BY symbol
        ''',
        (trade_date,),
        ).fetchall()
    ]
    symbols = [row['symbol'] for row in pool_rows]
    entry_parent_ids = latest_entry_parent_ids(conn, symbols)
    stock_klines = {symbol: akshare.stock_kline(symbol, trade_date=trade_date, limit=60) for symbol in symbols}
    active_deltas = (
        akshare.stock_active_deltas(symbols)
        if trade_date == date.today().isoformat()
        else {}
    )
    spot_rows = akshare.stock_spot_quotes(symbols)
    name_map = {
        str(row['symbol']): str(row.get('name') or '')
        for row in spot_rows
        if row.get('symbol') and row.get('name')
    }
    themes = sorted({row['theme_label'] for row in pool_rows} - {'unknown'})
    # 盘中跑 plan 时东财板块日 K 会带当日实时 bar；按 signal_date 过滤，防止"超前"校验拒绝
    sector_klines = {
        theme: akshare.sector_kline(theme, limit=60, trade_date=trade_date)
        for theme in themes
    }
    config = load_risk_config(config_path)
    payload = build_market_input(
        pool_rows,
        akshare.index_kline('sh000001', trade_date=trade_date, limit=60),
        akshare.lhb_rows(trade_date),
        stock_klines,
        sector_klines,
        regulator_list=config['regulator_list'],
        lhb_dumpers=config['lhb_dumpers'],
        tail_prices=load_tail_prices(conn, trade_date),
        now=date.fromisoformat(trade_date),
        active_retest=active_retest_symbols(conn),
        recent_stops=recent_stop_counts(
            conn, trade_date, config['same_ma_cooldown_days'], config['holidays']
        ),
        same_ma_cooldown_days=config['same_ma_cooldown_days'],
        two_source_deviation_max=float(config.get('guard', {}).get('two_source_deviation_max', 0.005)),
        quote_sanity_enabled=config.get('guard', {}).get('quote_sanity', 'enabled') == 'enabled',
        entry_parent_ids=entry_parent_ids,
        watchlist_limit=(
            int(config.get('watchlist_limit'))
            if config.get('watchlist_limit') is not None
            else None
        ),
        priority_order=(
            config['priority'] if config.get('priority') else None
        ),
        holidays=config['holidays'],
        name_map=name_map,
        active_deltas=active_deltas,
        # 量能口径=两市合计：深市成交额必须参与，档位不得只用沪市（§3.5）
        sz_index_rows=akshare.index_kline(SZ_AMOUNT_INDEX, trade_date=trade_date, limit=60),
    )
    available_sectors = {
        str(row['symbol']) for row in payload.get('sector_context') or []
    }
    index_summary = [{
        'symbol': 'sh000001',
        'close': payload['market']['sh_close'],
        'ma5': payload['market']['sh_ma5'],
    }]
    for context_index in config['context_indices']:
        if context_index == 'sh000001' or any(
            str(row.get('symbol')) == context_index
            for row in payload.get('index_context') or []
        ):
            continue
        context_rows = akshare.index_kline(context_index, trade_date=trade_date, limit=60)
        context_zone = _zone(
            context_rows,
            5,
        )
        if context_zone:
            payload.setdefault('index_context', []).append({
                'symbol': context_index,
                'zone': context_zone,
            })
        context_closes = _closes(context_rows)
        if context_closes:
            index_summary.append({
                'symbol': context_index,
                'close': context_closes[-1],
                'ma5': moving_average(context_closes, 5),
            })
    payload['market']['index_summary'] = index_summary
    latest_marks: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        '''
        SELECT date, code, level_low, level_high, label
        FROM context_marks
        WHERE context_type='sector' AND status='ACTIVE' AND date <= ?
        ORDER BY date, created_at
        ''',
        (trade_date,),
    ).fetchall():
        code = str(row['code'])
        latest_marks[code] = row
    for theme in themes:
        if theme in available_sectors or theme not in latest_marks:
            continue
        mark = latest_marks[theme]
        payload.setdefault('sector_context', []).append({
            'symbol': theme,
            'zone': [float(mark['level_low']), float(mark['level_high'])],
            'label': str(mark['label'] or theme),
            'data_status': 'STALE',
        })
    return payload


def signal_day_trigger_review(
    conn: sqlite3.Connection,
    trade_date: str,
    event_source: str | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            COALESCE(t.signal_id, s.id) AS id,
            COALESCE(t.symbol, s.symbol) AS symbol,
            COALESCE(t.strategy_id, s.strategy_id) AS strategy_id,
            COALESCE(t.tag, s.tag) AS tag,
            COALESCE(t.side, s.side) AS side,
            s.status,
            COALESCE(t.zone_low, s.zone_low) AS zone_low,
            COALESCE(t.zone_high, s.zone_high) AS zone_high,
            COALESCE(t.parent_id, s.parent_id) AS parent_id,
            COALESCE(t.root_id, s.root_id) AS root_id,
            COALESCE(t.created_trade_date, s.created_trade_date) AS created_trade_date,
            COALESCE(t.window_days, s.window_days) AS window_days,
            COALESCE(t.created_at, s.created_at) AS signal_created_at,
            COALESCE(t.pair_id, s.pair_id) AS pair_id,
            COALESCE(t.event_source, j.event_source) AS event_source,
            j.created_at AS triggered_at
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        LEFT JOIN signal_trigger_snapshots t
          ON t.signal_id = s.id AND t.triggered_at = j.created_at
        WHERE j.to_status='TRIGGERED' AND j.created_at LIKE ?
          AND j.event_source NOT IN ('replay','backfill')
        ORDER BY j.created_at, s.id
        ''',
        (trade_date + '%',),
    ).fetchall()

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        if str(row['id']) in {item['signal_id'] for item in reviewed}:
            continue
        mechanical = str(row['side']) == 'SELL'
        reviewed.append({
            'signal_id': str(row['id']),
            'symbol': str(row['symbol']),
            'strategy_id': str(row['strategy_id']),
            'tag': str(row['tag']),
            'zone_low': row['zone_low'],
            'zone_high': row['zone_high'],
            'parent_id': row['parent_id'],
            'root_id': row['root_id'],
            'created_trade_date': row['created_trade_date'],
            'window_days': row['window_days'],
            'created_at': row['signal_created_at'],
            'pair_id': row['pair_id'],
            'triggered_at': str(row['triggered_at']),
            'event_source': row['event_source'],
        })
    return reviewed


def anomalous_settlement_symbols(conn: sqlite3.Connection, trade_date: str) -> set[str]:
    '''§7.4 ANOMALOUS 行：当日结算 data_status=ANOMALOUS（定性待补）的 symbol 集合。'''
    rows = conn.execute(
        '''
        SELECT DISTINCT s.symbol
        FROM stop_settlements st
        JOIN signals s ON s.id = st.signal_id
        WHERE st.trade_date=? AND UPPER(st.data_status)='ANOMALOUS'
        ''',
        (trade_date,),
    ).fetchall()
    return {str(row['symbol']) for row in rows}


def run_evening(
    conn: sqlite3.Connection,
    input_path: str | Path,
    report_date: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding='utf-8'))
    trade_date = str(payload['trade_date'])
    created_at = datetime.fromisoformat(str(payload.get('created_at') or trade_date + 'T15:30:00+08:00'))
    strategies_path = payload.get('strategies_path') or Path(__file__).parents[2] / 'config' / 'strategies.yaml'

    from ..core.generator import load_strategies

    strategies = {strategy.id: strategy for strategy in load_strategies(strategies_path)}
    existing_signals = load_signals(conn)
    protection_states = pool_protection_states(conn)
    # §7.4：BLOCKED（回踩观察中）暂停普通 enter；WAIT_REENTRY 正是“等待新 enter”，允许生成
    blocked_symbols = {
        symbol
        for symbol, (mode, _reason) in protection_states.items()
        if mode == 'BLOCKED'
    }
    paused_symbols = {
        symbol
        for symbol, (mode, _reason) in protection_states.items()
        if mode != 'NORMAL'
    }
    # §7.4 ANOMALOUS 行：定性待补的破位票维持池内保护，但抑制新 entry，
    # 直至定性补齐或人工结案（结算落定后 data_status 不再是 ANOMALOUS）
    anomalous_symbols = anomalous_settlement_symbols(conn, trade_date)
    plans = list(payload.get('plans') or [])
    assignments = list(payload.get('assignments') or [])
    holdings = [
        row for row in (payload.get('holdings') or [])
        if str(row.get('symbol') or '') not in paused_symbols
    ]
    # §5 t_overlay.applies_to=pool：波动超限的持仓票禁 T，由池内路径统一执行
    volatile_symbols = {
        str(row.get('symbol') or '')
        for row in holdings
        if row.get('volatility_exceeded') and row.get('symbol')
    }
    pool_symbols = {
        str(row.get('symbol') or '')
        for row in (payload.get('signal_rows') or [])
        if row.get('symbol')
    }
    # 同票不再走双路径：有 K 线（signal_rows 覆盖）的持仓票由池内路径统一生成 Z/W/T，
    # holding 行退为缺 K 线票的兜底，避免 {date}-{symbol}-stop 同 id 静默覆盖
    holding_rows = build_holding_rows(
        [row for row in holdings if str(row.get('symbol') or '') not in pool_symbols],
        created_at,
    )
    generation_rejections: list[dict[str, str]] = []
    if assignments:
        plans.extend(build_pool_plans(strategies.values(), assignments, generation_rejections))
    filtered_plans: list[dict[str, Any]] = []
    for plan in plans:
        symbol = str(plan.get('symbol') or '')
        if symbol in blocked_symbols:
            generation_rejections.append({'symbol': symbol, 'reason': 'blocked_protection'})
        elif symbol in anomalous_symbols:
            generation_rejections.append({'symbol': symbol, 'reason': 'anomalous_stop_pending'})
        else:
            filtered_plans.append(plan)
    plans = filtered_plans
    rows = build_evening_signals(
        strategies,
        plans,
        str(payload.get('market_tier') or 'NORMAL'),
        created_at,
        existing_signals,
        payload.get('recent_stop_counts') or {},
        int(payload.get('same_ma_cooldown_days') or 2),
        skip_zw_symbols={
            str(row.get('symbol') or '')
            for row in (payload.get('signal_rows') or [])
            if row.get('symbol')
        },
        rejections=generation_rejections,
    )
    cooled_candidates = expire_cooled_candidates(
        existing_signals,
        payload.get('recent_stop_counts') or {},
        created_at,
    )
    rows.extend(cooled_candidates)
    rows.extend(holding_rows)
    rows.extend(build_pool_signals(
        [
            row for row in (payload.get('signal_rows') or [])
            if str(row.get('symbol') or '') not in paused_symbols
        ],
        created_at,
        existing_signals,
        suppress_t_symbols=volatile_symbols,
    ))
    valid_for = next_trading_day(
        date.fromisoformat(trade_date),
        load_holidays(payload.get('holidays')),
    )
    rows = [
        signal.model_copy(update={
            **(
                {'valid_for': valid_for}
                if signal.signal_role in {SignalRole.ENTRY, SignalRole.T}
                else {}
            ),
            **(
                {'pair_id': f'{signal.parent_id}:{valid_for.isoformat()}'}
                if signal.signal_role is SignalRole.T
                else {}
            ),
        })
        for signal in rows
    ]
    context_marks = build_context_marks(
        payload.get('index_context') or [],
        payload.get('sector_context') or [],
        date.fromisoformat(trade_date),
        created_at,
    )
    conn.execute(
        "UPDATE context_marks SET status='INACTIVE' WHERE date < ? AND status='ACTIVE'",
        (trade_date,),
    )
    save_context_marks(conn, context_marks)
    save_event_calendar(conn, build_event_calendar(
        strategies, plans, date.fromisoformat(trade_date), created_at,
    ))
    quality_rows = [
        dict(row)
        for row in conn.execute(
            'SELECT id, symbol, strategy_id, tag, role, status, zone_low, zone_high, '
            'created_at, valid_for, closed_at FROM signals'
        ).fetchall()
    ]
    market_rows = [
        {
            **{key: assignment.get(key) for key in ('symbol', 'est_close', 'low', 'high', 'two_source_anomaly')},
            'trade_date': trade_date,
            'volume_ratio': assignment.get('volume_ratio'),
        }
        for assignment in assignments
    ]
    save_market_snapshots(conn, trade_date, market_rows)
    # §9：EV_NEG 输入链——entry 假想成交于有效日收盘，window 个交易日后收盘估值
    pnl_rows = attach_hypothetical_pnl(quality_rows, load_market_snapshots(conn))
    ev_negative = {
        strategy_id
        for strategy_id, quality in signal_quality(pnl_rows, market_rows).items()
        if quality['ev_negative']
    }
    rows = [
        signal.model_copy(update={'premise': {**signal.premise, 'ev_negative': True}})
        if signal.strategy_id in ev_negative
        else signal
        for signal in rows
    ]
    save_signals(conn, rows)
    # 当晚生成的终态行统一落 journal（冷却/T 归零/trend_flip），此前只覆盖冷却一个来源
    existing_by_id = {signal.id: signal for signal in existing_signals}
    for signal in rows:
        if signal.status not in {SignalStatus.EXPIRED, SignalStatus.INVALIDATED}:
            continue
        before = existing_by_id.get(signal.id)
        if before is not None and before.status is not SignalStatus.ACTIVE:
            continue
        save_journal(conn, JournalEntry(
            signal_id=signal.id,
            from_status=(before.status.value if before else SignalStatus.ACTIVE.value),
            to_status=signal.status.value,
            event='eod',
            reason=signal.status_reason or 'eod',
            created_at=created_at,
            event_source='live_settle',
        ))
    snapshot_signals = [
        signal for signal in load_signals(conn)
        if signal.status is SignalStatus.ACTIVE
        or (
            signal.closed_at is not None
            and signal.closed_at.date() >= date.fromisoformat(trade_date)
        )
    ]
    save_report_signal_snapshot(
        conn,
        report_date or trade_date,
        snapshot_signals,
    )
    conn.commit()
    return {
        'trade_date': trade_date,
        'report_date': report_date or trade_date,
        'signal_ids': [signal.id for signal in rows],
        'generation_rejections': generation_rejections,
    }
