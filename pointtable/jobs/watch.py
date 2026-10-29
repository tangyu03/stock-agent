from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ..core.generator import order_for_push
from ..core.lifecycle import JournalEntry, TransitionEvent, transition
from ..core.volume import Activity, BreakQualifier, active_or_passive, break_qualifier
from ..models.signal import Signal, SignalRole, SignalSide, SignalStatus, SignalTag
import sqlite3
from datetime import timedelta
from ..db.session import (
    load_active_signals, load_context_marks, load_pending_correction_anchors,
    mark_correction_notice, save_correction_event, save_journal, save_signal,
    touch_context_mark,
)
from ..models.context_mark import ContextType
from ..models.journal import JournalRecordType
from .settle import correction_notice, post_break_correction_notice


def confirm_touch(
    signal: Signal,
    price: float,
    *,
    now: datetime | None = None,
    volume_ratio: float | None = None,
    retest_volume_min: float = 1.2,
) -> tuple[Signal | None, str]:
    if signal.tag is SignalTag.RETEST_CONFIRM:
        if (
            price >= signal.zone_high
            and volume_ratio is not None
            and volume_ratio >= retest_volume_min
        ):
            return signal, 'retest_pass'
        if signal.zone_low <= price < signal.zone_high:
            # §7.5：盘中入区只是事实，确认破位须收盘未站上上沿，由 settle 结案
            return signal, 'retest_touch'
        return None, 'not_in_zone'
    in_zone = signal.zone_low <= price <= signal.zone_high
    if not in_zone:
        return None, 'not_in_zone'
    if (
        signal.signal_role is SignalRole.ENTRY
        and signal.trigger_window_start is not None
        and signal.trigger_window_end is not None
    ):
        current_time = (now or datetime.now()).time()
        if not signal.trigger_window_start <= current_time <= signal.trigger_window_end:
            return None, 'outside_trigger_window'
    if signal.signal_role is SignalRole.ENTRY:
        return signal, 'entry_single_point'
    if signal.side is SignalSide.SELL or signal.tag is SignalTag.STOP:
        return signal, 'stop_single_point'
    return signal, 'single_point'


def watch_signals(signals: Iterable[Signal], price: float, second_cycle: bool) -> list[Signal]:
    active = [signal for signal in signals if signal.status is SignalStatus.ACTIVE]
    return order_for_push(active)


def qualify_break(
    *,
    volume_ratio: float,
    active_delta: float | None,
    stock_triggered: bool,
    sector_triggered: bool,
    index_triggered: bool,
    divergence: bool = False,
    limit_up: bool = False,
) -> BreakQualifier:
    decision = break_qualifier(volume_ratio, active_delta, divergence, limit_up)
    if decision.qualifier is BreakQualifier.LIMIT_EXEMPT:
        return BreakQualifier.LIMIT_EXEMPT
    if decision.qualifier is BreakQualifier.DIVERGENCE:
        return BreakQualifier.DIVERGENCE
    if decision.qualifier is BreakQualifier.EXECUTE_NOW:
        return BreakQualifier.EXECUTE_NOW
    if decision.qualifier is BreakQualifier.UNCLASSIFIED:
        return BreakQualifier.UNCLASSIFIED
    activity = active_or_passive(stock_triggered, sector_triggered, index_triggered)
    if activity is Activity.ACTIVE:
        return BreakQualifier.EXECUTE_NOW
    return BreakQualifier.DELAY_OBSERVE


def stop_break_decision(context: Mapping[str, Any] | None) -> tuple[BreakQualifier, str]:
    if not context:
        return BreakQualifier.UNCLASSIFIED, 'NOT_CLASSIFIED'
    decision = break_qualifier(
        float(context['volume_ratio']) if context.get('volume_ratio') is not None else None,
        float(context['active_delta']) if context.get('active_delta') is not None else None,
        bool(context.get('divergence', False)),
        bool(context.get('limit_up', False)),
    )
    if decision.qualifier is BreakQualifier.EXECUTE_NOW:
        return decision.qualifier, decision.reason
    if decision.qualifier is BreakQualifier.UNCLASSIFIED:
        return decision.qualifier, decision.reason
    if decision.qualifier in {BreakQualifier.LIMIT_EXEMPT, BreakQualifier.DIVERGENCE}:
        return decision.qualifier, decision.reason
    activity = active_or_passive(
        bool(context.get('stock_triggered', False)),
        bool(context.get('sector_triggered', False)),
        bool(context.get('index_triggered', False)),
    )
    return (
        BreakQualifier.EXECUTE_NOW if activity is Activity.ACTIVE else BreakQualifier.DELAY_OBSERVE,
        decision.reason,
    )


def record_retest_touch(signal: Signal, price: float) -> Signal:
    '''§7.5 判决事实：入区事实与期间最高价持续累积在 premise。'''
    observation = dict(signal.premise.get('retest_observation') or {})
    observation['ever_touched'] = True
    observation['max_high'] = max(float(observation.get('max_high') or 0.0), float(price))
    return signal.model_copy(update={
        'premise': {**signal.premise, 'retest_observation': observation},
    })


def retest_verdict_facts(
    signal: Signal,
    *,
    outcome: str,
    now: datetime,
    close: float | None = None,
    volume_ratio: float | None = None,
    window_days: int | None = None,
    observed_days: int | None = None,
) -> dict[str, Any]:
    observation = dict(signal.premise.get('retest_observation') or {})
    facts: dict[str, Any] = {
        'outcome': outcome,
        'judged_at': now.isoformat(timespec='seconds'),
        'judged_date': now.date().isoformat(),
        'window_start': signal.created_at.date().isoformat(),
        'window_end': now.date().isoformat(),
        'zone_low': signal.zone_low,
        'zone_high': signal.zone_high,
        'ever_touched': bool(observation.get('ever_touched')),
        'close': close,
    }
    if observation.get('max_high') is not None:
        facts['max_high'] = observation.get('max_high')
    if volume_ratio is not None:
        facts['volume_ratio'] = volume_ratio
    if window_days is not None:
        facts['window_days'] = window_days
    if observed_days is not None:
        facts['observed_days'] = observed_days
    return facts


def invalidate_t_buy_on_stop(
    conn: sqlite3.Connection,
    stop_signal: Signal,
    now: datetime,
) -> list[str]:
    '''§4 区间不变量4：stop 盘中 TRIGGERED 后，同票未触发的 ACTIVE T_BUY 连坐 INVALIDATED。'''
    invalidated: list[str] = []
    for row in load_active_signals(conn):
        if (
            row.symbol == stop_signal.symbol
            and row.signal_role is SignalRole.T
            and row.side is SignalSide.BUY
        ):
            updated, journal = transition(
                row, TransitionEvent.PREMISE_BROKEN, 'stop_triggered', now=now
            )
            save_signal(conn, updated)
            save_journal(conn, journal)
            invalidated.append(updated.id)
    return invalidated


def process_touch(
    signal: Signal,
    price: float,
    *,
    now: datetime | None = None,
    volume_ratio: float | None = None,
    retest_volume_min: float = 1.2,
) -> tuple[Signal | None, JournalEntry | None, str]:
    touched, reason = confirm_touch(
        signal,
        price,
        now=now,
        volume_ratio=volume_ratio,
        retest_volume_min=retest_volume_min,
    )
    if touched is None:
        return None, None, reason
    if reason == 'retest_touch':
        return record_retest_touch(signal, price), None, reason
    event = (
        TransitionEvent.PREMISE_BROKEN
        if reason == 'retest_pass'
        else TransitionEvent.HIT
    )
    updated, journal = transition(
        signal, event, reason, price, now=now
    )
    if reason == 'retest_pass':
        updated = updated.model_copy(update={'premise': {
            **updated.premise,
            'verdict_facts': retest_verdict_facts(
                updated,
                outcome='INVALIDATED',
                now=now or datetime.now(),
                close=price,
                volume_ratio=volume_ratio,
            ),
        }})
    return updated, journal, reason


def invalidate_premise(
    signal: Signal,
    now: datetime | None = None,
) -> tuple[Signal, JournalEntry]:
    return transition(signal, TransitionEvent.PREMISE_BROKEN, now=now)
def context_from_marks(conn: sqlite3.Connection, symbol: str, now: datetime) -> dict[str, bool]:
    trade_date = now.date().isoformat()
    day_start = f'{trade_date}T00:00:00'
    day_end = now.isoformat(timespec='seconds')
    pool_row = conn.execute(
        '''
        SELECT theme_label FROM pool
        WHERE date <= ? AND symbol=?
        ORDER BY date DESC LIMIT 1
        ''',
        (trade_date, symbol),
    ).fetchone()

    def mark_triggered(context_type: ContextType, code: str) -> bool:
        row = conn.execute(
            '''
            SELECT 1 FROM context_marks
            WHERE date=? AND context_type=? AND code=?
              AND COALESCE(data_status, 'OK') = 'OK'
              AND label <> 'stale_sector_context'
              AND triggered_at IS NOT NULL AND triggered_at >= ? AND triggered_at <= ?
            LIMIT 1
            ''',
            (trade_date, context_type.value, code, day_start, day_end),
        ).fetchone()
        return row is not None

    sector_code = str(pool_row['theme_label']) if pool_row else ''
    return {
        'stock_triggered': False,
        'sector_triggered': bool(sector_code and mark_triggered(ContextType.SECTOR, sector_code)),
        'index_triggered': mark_triggered(ContextType.INDEX, 'sh000001'),
    }



def run_watch(
    conn: sqlite3.Connection,
    price: float,
    second_cycle: bool,
    now: datetime,
    volume_ratio: float | None = None,
    trade_date: str | None = None,
    broken_signal_ids: Iterable[str] | None = None,
    break_context: Mapping[str, Mapping[str, Any]] | None = None,
    retest_volume_min: float = 1.2,
    prices: Mapping[str, float] | None = None,
    context_prices: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    touched: list[str] = []
    invalidated: list[str] = []
    cascaded_ids: set[str] = set()
    broken_ids = set(broken_signal_ids or ())
    break_decisions: list[dict[str, str]] = []
    correction_notices: list[str] = []
    if context_prices is not None:
        for mark in load_context_marks(conn, now.date().isoformat()):
            price = (context_prices.get(mark.context_type.value) or {}).get(mark.code)
            if price is not None and mark.level_low <= price <= mark.level_high:
                touch_context_mark(conn, mark.date.isoformat(), mark.context_type, mark.code, now)
        conn.commit()
    for signal in load_active_signals(conn):
        if signal.id in cascaded_ids:
            continue
        if (
            signal.signal_role in {SignalRole.ENTRY, SignalRole.T}
            and signal.valid_for is not None
            and now.date() != signal.valid_for
        ):
            continue
        signal_price = (prices or {}).get(signal.symbol) if prices is not None else price
        if signal_price is None:
            continue
        if signal.id in broken_ids:
            updated, journal = invalidate_premise(signal, now)
            save_signal(conn, updated)
            save_journal(conn, journal)
            invalidated.append(signal.id)
            continue
        decision: tuple[BreakQualifier, str] | None = None
        if signal.tag is SignalTag.STOP:
            context = (break_context or {}).get(signal.symbol)
            if context is None:
                context = context_from_marks(conn, signal.symbol, now)
            decision = stop_break_decision(context)
        updated, journal, reason = process_touch(
            signal,
            signal_price,
            now=now,
            volume_ratio=volume_ratio,
            retest_volume_min=retest_volume_min,
        )
        if updated is not None and journal is None:
            # 盘中入区（retest_touch）：只累积判决事实，不迁移状态
            save_signal(conn, updated)
            touched.append(signal.id)
            continue
        if updated is not None and journal is not None:
            journal = journal.model_copy(update={'event_source': 'live_watch'})
            if decision is not None:
                updated = updated.model_copy(update={'break_qualifier': decision[0].value})
                qualified_reason = f"{reason}:{decision[0].value}:{decision[1]}"
                updated = updated.model_copy(update={'status_reason': qualified_reason})
                journal = journal.model_copy(update={'reason': qualified_reason})
                break_decisions.append({
                    'signal_id': signal.id,
                    'qualifier': decision[0].value,
                    'reason': decision[1],
                })
            save_signal(conn, updated)
            save_journal(conn, journal)
            if signal.tag is SignalTag.STOP and decision is not None:
                save_journal(conn, journal.model_copy(update={
                    'record_type': JournalRecordType.QUALIFIER,
                    'reason': decision[0].value,
                }))
            touched.append(signal.id)
            if (
                signal.tag is SignalTag.STOP
                and updated.status is SignalStatus.TRIGGERED
            ):
                cascaded_ids.update(invalidate_t_buy_on_stop(conn, updated, now))
            if reason == 'retest_pass':
                
                notice = correction_notice(updated, 'retest_pass')
                save_correction_event(
                    conn,
                    trade_date=now.date().isoformat(),
                    signal_id=updated.id,
                    symbol=updated.symbol,
                    trigger='retest_pass',
                    notice=notice,
                    created_at=now,
                    scene='retest_invalidated',
                    dedup_key=f'{updated.id}:{now.date().isoformat()}:retest_invalidated',
                    event_source='live_watch',
                )
                correction_notices.append(notice)
    
    for anchor in load_pending_correction_anchors(conn):
        signal_price = (prices or {}).get(anchor['symbol']) if prices is not None else price
        if (
            signal_price is None
            or now.date().isoformat() <= anchor['trade_date']
            or not anchor['zone_low'] <= signal_price <= anchor['zone_high']
        ):
            continue
        if mark_correction_notice(conn, anchor['signal_id'], signal_price, now):
            notice = post_break_correction_notice(anchor)
            save_correction_event(
                conn,
                trade_date=now.date().isoformat(),
                signal_id=anchor['signal_id'],
                symbol=anchor['symbol'],
                trigger='post_break_retest',
                notice=notice,
                created_at=now,
                scene='after_execute_now',
                dedup_key=f"{anchor['signal_id']}:{now.date().isoformat()}:after_execute_now",
                event_source='live_watch',
            )
            correction_notices.append(notice)
    conn.commit()
    return {
        'touched': touched,
        'premise_broken': invalidated,
        'break_decisions': break_decisions,
        'correction_notices': correction_notices,
    }
