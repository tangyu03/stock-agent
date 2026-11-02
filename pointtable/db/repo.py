"""仓储层：各表按行的 save_*/load_* 与行→模型还原。

从 db/session.py 迁出（批次4）：session.py 只保留连接生命周期与 schema 迁移
（set_database_path/get_connection/init_db/SCHEMA），所有按表读写、行装配的
函数收拢于此，统一以调用方传入的 conn 为入口（不自行取连接）。db.session
顶部 re-export 全部公开名，既有 `from ..db.session import ...` 调用方
（jobs/stats/tests）零改动。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time
from typing import Any

from ..models.context_mark import ContextMark, ContextStatus, ContextType
from ..models.event_calendar import EventCalendar
from ..models.journal import JournalEntry
from ..models.signal import Signal, SignalStatus


def save_signal(conn: sqlite3.Connection, signal: Signal) -> None:
    conn.execute(
        '''INSERT OR REPLACE INTO signals (
               id, symbol, strategy_id, side, tag, role, zone_low, zone_high, size_hint,
               premise, break_qualifier, settlement_outcome, trend, activation,
               valid_policy, valid_for, pair_id, root_id, created_trade_date, window_days,
               status, status_reason, parent_id, created_at, closed_at,
               trigger_window_start, trigger_window_end
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id, signal.symbol, signal.strategy_id, signal.side.value,
            signal.tag.value, signal.signal_role.value, signal.zone_low, signal.zone_high, signal.size_hint,
            json.dumps(signal.premise, ensure_ascii=False), signal.break_qualifier, signal.settlement_outcome,
            signal.trend.value, signal.activation,
            signal.valid_policy.value,
            signal.valid_for.isoformat() if signal.valid_for else None,
            signal.pair_id,
            signal.root_id,
            signal.created_trade_date.isoformat() if signal.created_trade_date else None,
            signal.window_days,
            signal.status.value, signal.status_reason,
            signal.parent_id, signal.created_at.isoformat(timespec='seconds'),
            signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
            signal.trigger_window_start.isoformat(timespec='seconds') if signal.trigger_window_start else None,
            signal.trigger_window_end.isoformat(timespec='seconds') if signal.trigger_window_end else None,
        ),
    )
    save_zone_snapshot(
        conn,
        signal.id,
        signal.zone_low,
        signal.zone_high,
        signal.created_at,
    )


def save_trigger_snapshot(
    conn: sqlite3.Connection,
    signal: Signal,
    triggered_at: datetime,
    event_source: str = 'live_settle',
) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO signal_trigger_snapshots (
               signal_id, triggered_at, symbol, strategy_id, side, tag, role,
               zone_low, zone_high, parent_id, root_id, created_trade_date, window_days, created_at, valid_policy,
               valid_for, pair_id, event_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal.id,
            triggered_at.isoformat(timespec='seconds'),
            signal.symbol,
            signal.strategy_id,
            signal.side.value,
            signal.tag.value,
            signal.signal_role.value,
            signal.zone_low,
            signal.zone_high,
            signal.parent_id,
            signal.root_id,
            signal.created_trade_date.isoformat() if signal.created_trade_date else None,
            signal.window_days,
            signal.created_at.isoformat(timespec='seconds'),
            signal.valid_policy.value,
            signal.valid_for.isoformat() if signal.valid_for else None,
            signal.pair_id,
            event_source,
        ),
    )


def save_signals(conn: sqlite3.Connection, signals: list[Signal]) -> int:
    for signal in signals:
        save_signal(conn, signal)
    return len(signals)


def save_zone_snapshot(
    conn: sqlite3.Connection,
    signal_id: str,
    zone_low: float,
    zone_high: float,
    effective_at: datetime,
) -> None:
    conn.execute(
        '''INSERT OR IGNORE INTO zone_snapshots (
               signal_id, effective_at, zone_low, zone_high, created_at
           ) VALUES (?,?,?,?,?)''',
        (
            signal_id,
            effective_at.isoformat(timespec='seconds'),
            float(zone_low),
            float(zone_high),
            datetime.now().astimezone().isoformat(timespec='seconds'),
        ),
    )


def load_zone_snapshot(
    conn: sqlite3.Connection,
    signal_id: str,
    effective_at: str,
) -> tuple[float, float] | None:
    row = conn.execute(
        '''
        SELECT zone_low, zone_high FROM zone_snapshots
        WHERE signal_id=? AND effective_at<=?
        ORDER BY effective_at DESC LIMIT 1
        ''',
        (signal_id, effective_at),
    ).fetchone()
    if row is None:
        return None
    return float(row['zone_low']), float(row['zone_high'])


def save_market_snapshots(
    conn: sqlite3.Connection,
    trade_date: str,
    rows: list[dict[str, Any]],
) -> int:
    for row in rows:
        conn.execute(
            '''INSERT OR REPLACE INTO market_snapshots (
                   trade_date, symbol, est_close, low, high, volume_ratio,
                   two_source_anomaly, created_at
               ) VALUES (?,?,?,?,?,?,?,?)''',
            (
                trade_date,
                str(row['symbol']),
                float(row['est_close']),
                float(row['low']) if row.get('low') is not None else None,
                float(row['high']) if row.get('high') is not None else None,
                float(row['volume_ratio']) if row.get('volume_ratio') is not None else None,
                1 if row.get('two_source_anomaly') else 0,
                datetime.now().astimezone().isoformat(timespec='seconds'),
            ),
        )
    return len(rows)


def load_market_snapshots(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
) -> list[dict[str, Any]]:
    if trade_date:
        rows = conn.execute(
            'SELECT * FROM market_snapshots WHERE trade_date=? ORDER BY symbol',
            (trade_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM market_snapshots ORDER BY trade_date, symbol'
        ).fetchall()
    return [
        {
            'trade_date': str(row['trade_date']),
            'symbol': str(row['symbol']),
            'est_close': row['est_close'],
            'low': row['low'],
            'high': row['high'],
            'volume_ratio': row['volume_ratio'],
            'two_source_anomaly': bool(row['two_source_anomaly']),
        }
        for row in rows
    ]


def save_market_day(
    conn: sqlite3.Connection,
    trade_date: str,
    market_tier: str,
    index_trend: str,
    *,
    evaluated_at: datetime,
    rule_version: str = 'circuit-v1',
    volume_tier: str = 'LOW',
    index_trend_rule: str = 'DOWN',
) -> None:
    from ..core.volume import is_circuit_day

    conn.execute(
        '''INSERT INTO market_days (
               trade_date, market_tier, index_trend, circuit_day,
               evaluated_at, rule_version
           ) VALUES (?,?,?,?,?,?)
           ON CONFLICT(trade_date) DO UPDATE SET
               market_tier=excluded.market_tier,
               index_trend=excluded.index_trend,
               circuit_day=excluded.circuit_day,
               evaluated_at=excluded.evaluated_at,
               rule_version=excluded.rule_version''',
        (
            trade_date,
            market_tier,
            index_trend,
            1 if is_circuit_day(
                market_tier,
                index_trend,
                volume_tier=volume_tier,
                index_trend_rule=index_trend_rule,
            ) else 0,
            evaluated_at.isoformat(timespec='seconds'),
            rule_version,
        ),
    )


def load_market_day(conn: sqlite3.Connection, trade_date: str) -> dict[str, Any] | None:
    row = conn.execute('SELECT * FROM market_days WHERE trade_date=?', (trade_date,)).fetchone()
    if row is None:
        return None
    return {
        'trade_date': str(row['trade_date']),
        'market_tier': str(row['market_tier']),
        'index_trend': str(row['index_trend']),
        'circuit_day': bool(row['circuit_day']),
        'evaluated_at': str(row['evaluated_at']),
        'rule_version': str(row['rule_version']),
    }


def save_stop_settlement(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    trade_date: str,
    raw_qualifier: str | None,
    effective_qualifier: str,
    circuit_day: bool,
    close_price: float | None,
    zone_low: float,
    zone_high: float,
    settlement_outcome: str,
    occurred_at: datetime,
    correction_scene: str | None = None,
    event_source: str = 'live_settle',
    data_status: str = 'OK',
    code_version: str = 'local',
    input_snapshot_id: str | None = None,
) -> bool:
    recorded_at = datetime.now().astimezone().isoformat(timespec='seconds')
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO stop_settlements (
               signal_id, trade_date, raw_qualifier, effective_qualifier, circuit_day,
               close_price, zone_low, zone_high, zone_snapshot_id, settlement_outcome,
               data_status, correction_scene, event_source, occurred_at, recorded_at,
               code_version, input_snapshot_id, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            signal_id, trade_date, raw_qualifier, effective_qualifier,
            1 if circuit_day else 0, close_price, zone_low, zone_high,
            signal_id + ':' + occurred_at.isoformat(timespec='seconds'),
            settlement_outcome, data_status, correction_scene, event_source,
            occurred_at.isoformat(timespec='seconds'), recorded_at,
            code_version, input_snapshot_id,
            recorded_at,
        ),
    )
    return cursor.rowcount > 0


def save_report_signal_snapshot(
    conn: sqlite3.Connection,
    report_date: str,
    signals: list[Signal],
) -> int:
    for signal in signals:
        conn.execute(
            '''INSERT OR REPLACE INTO report_signal_snapshots (
                   report_date, signal_id, symbol, strategy_id, side, tag, role,
                   zone_low, zone_high, size_hint, premise, break_qualifier,
                   settlement_outcome, trend, activation, valid_policy,
                   valid_for, pair_id, root_id, created_trade_date, window_days,
                   status, status_reason, parent_id, created_at, closed_at,
                   trigger_window_start, trigger_window_end
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                report_date,
                signal.id,
                signal.symbol,
                signal.strategy_id,
                signal.side.value,
                signal.tag.value,
                signal.signal_role.value,
                signal.zone_low,
                signal.zone_high,
                signal.size_hint,
                json.dumps(signal.premise, ensure_ascii=False),
                signal.break_qualifier,
                signal.settlement_outcome,
                signal.trend.value,
                signal.activation,
                signal.valid_policy.value,
                signal.valid_for.isoformat() if signal.valid_for else None,
                signal.pair_id,
                signal.root_id,
                signal.created_trade_date.isoformat() if signal.created_trade_date else None,
                signal.window_days,
                signal.status.value,
                signal.status_reason,
                signal.parent_id,
                signal.created_at.isoformat(timespec='seconds'),
                signal.closed_at.isoformat(timespec='seconds') if signal.closed_at else None,
                signal.trigger_window_start.isoformat(timespec='seconds') if signal.trigger_window_start else None,
                signal.trigger_window_end.isoformat(timespec='seconds') if signal.trigger_window_end else None,
            ),
        )
    return len(signals)


def load_report_signal_snapshot(
    conn: sqlite3.Connection,
    report_date: str,
) -> list[Signal]:
    rows = conn.execute(
        'SELECT * FROM report_signal_snapshots WHERE report_date=? ORDER BY created_at, signal_id',
        (report_date,),
    ).fetchall()
    signals = []
    for row in rows:
        item = dict(row)
        item['id'] = item.pop('signal_id')
        item.pop('report_date')
        item.pop('event_source', None)
        signals.append(signal_from_row(item))
    return signals


def save_journal(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    if entry.to_status == 'TRIGGERED':
        signal_row = conn.execute(
            'SELECT * FROM signals WHERE id=?', (entry.signal_id,)
        ).fetchone()
        if signal_row is not None:
            save_trigger_snapshot(
                conn,
                signal_from_row(signal_row),
                entry.created_at,
                event_source=entry.event_source,
            )
    conn.execute(
        '''INSERT OR IGNORE INTO journal (
               signal_id, from_status, to_status, reason, price,
               event, qualifier, old_zone_low, old_zone_high,
               new_zone_low, new_zone_high,
               record_type, event_source, created_at, occurred_at, recorded_at,
               zone_snapshot_id, run_id, code_version, input_snapshot_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            entry.signal_id, entry.from_status, entry.to_status, entry.reason,
            entry.price,
            entry.event, entry.qualifier,
            entry.old_zone[0] if entry.old_zone else None,
            entry.old_zone[1] if entry.old_zone else None,
            entry.new_zone[0] if entry.new_zone else None,
            entry.new_zone[1] if entry.new_zone else None,
            entry.record_type.value, entry.event_source,
            entry.created_at.isoformat(timespec='seconds'),
            (entry.occurred_at or entry.created_at).isoformat(timespec='seconds'),
            (entry.recorded_at or datetime.now().astimezone()).isoformat(timespec='seconds'),
            entry.zone_snapshot_id, entry.run_id, entry.code_version,
            entry.input_snapshot_id,
        ),
    )


def save_context_marks(conn: sqlite3.Connection, marks: list[ContextMark]) -> int:
    for mark in marks:
        conn.execute(
            '''INSERT OR REPLACE INTO context_marks (
                   date, context_type, code, level_low, level_high, label,
                   status, data_status, triggered_at, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                mark.date.isoformat(), mark.context_type.value, mark.code,
                mark.level_low, mark.level_high, mark.label, mark.status.value,
                mark.data_status,
                mark.triggered_at.isoformat(timespec='seconds') if mark.triggered_at else None,
                mark.created_at.isoformat(timespec='seconds'),
            ),
        )
    return len(marks)


def save_event_calendar(conn: sqlite3.Connection, events: list[EventCalendar]) -> int:
    for event in events:
        conn.execute(
            '''INSERT OR REPLACE INTO event_calendar (
                   date, event_type, code, label, due_time, payload, created_at
               ) VALUES (?,?,?,?,?,?,?)''',
            (
                event.date.isoformat(), event.event_type, event.code, event.label,
                event.due_time.isoformat(timespec='seconds'),
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at.isoformat(timespec='seconds'),
            ),
        )
    return len(events)


def load_event_calendar(
    conn: sqlite3.Connection,
    trade_date: str,
    event_type: str | None = 'time_trigger',
) -> list[EventCalendar]:
    sql = 'SELECT * FROM event_calendar WHERE date=?'
    params: list[object] = [trade_date]
    if event_type is not None:
        sql += ' AND event_type=?'
        params.append(event_type)
    sql += ' ORDER BY due_time, code'
    rows = []
    for row in conn.execute(sql, params).fetchall():
        rows.append(EventCalendar(
            date=date.fromisoformat(row['date']),
            event_type=str(row['event_type']),
            code=str(row['code']),
            label=str(row['label']),
            due_time=time.fromisoformat(row['due_time']),
            payload=json.loads(row['payload']),
            created_at=datetime.fromisoformat(row['created_at']),
        ))
    return rows


def save_correction_anchor(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    symbol: str,
    trade_date: str,
    qualifier: str,
    zone_low: float,
    zone_high: float,
    created_at: datetime,
) -> bool:
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO correction_anchors (
               signal_id, symbol, trade_date, qualifier,
               zone_low, zone_high, created_at
           ) VALUES (?,?,?,?,?,?,?)''',
        (
            signal_id, symbol, trade_date, qualifier,
            float(zone_low), float(zone_high),
            created_at.isoformat(timespec='seconds'),
        ),
    )
    return cursor.rowcount > 0


def save_correction_event(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    signal_id: str,
    symbol: str,
    trigger: str,
    notice: str,
    created_at: datetime,
    scene: str | None = None,
    source_signal_id: str | None = None,
    notice_status: str = 'pending',
    dedup_key: str | None = None,
    event_source: str = 'live_settle',
) -> bool:
    effective_scene = scene or ('same_day_recover' if trigger == 'same_day_close' else trigger)
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO correction_events (
               trade_date, signal_id, symbol, trigger, notice, created_at,
               scene, source_signal_id, notice_status, dedup_key, event_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (
            trade_date, signal_id, symbol, trigger, notice,
            created_at.isoformat(timespec='seconds'), effective_scene,
            source_signal_id or signal_id, notice_status, dedup_key,
            event_source,
        ),
    )
    return cursor.rowcount > 0


def load_correction_events(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''
            SELECT ce.trade_date, ce.signal_id, ce.symbol, ce.trigger,
                   ce.notice, ce.created_at, ce.scene, ce.source_signal_id,
                   ce.notice_status, ce.dedup_key, ce.event_source, s.strategy_id
            FROM correction_events
            ce
            JOIN signals s ON s.id = ce.signal_id
            WHERE ce.trade_date=?
            ORDER BY ce.created_at, ce.id
            ''',
            (trade_date,),
        ).fetchall()
    ]


def signal_from_row(row: sqlite3.Row | dict[str, Any]) -> Signal:
    item = dict(row)
    if isinstance(item.get('premise'), str):
        try:
            item['premise'] = json.loads(item['premise'])
        except json.JSONDecodeError:
            item['premise'] = {}
    if 'settlement_outcome' not in item:
        item['settlement_outcome'] = None
    return Signal.model_validate(item)


def load_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    sql = 'SELECT * FROM signals'
    params: tuple[str, ...] = ()
    if trade_date:
        sql += ' WHERE created_at LIKE ?'
        params = (trade_date + '%',)
    sql += ' ORDER BY created_at, id'
    return [signal_from_row(row) for row in conn.execute(sql, params).fetchall()]


def load_active_signals(conn: sqlite3.Connection, trade_date: str | None = None) -> list[Signal]:
    return [signal for signal in load_signals(conn, trade_date) if signal.status is SignalStatus.ACTIVE]


def load_stop_settlements(
    conn: sqlite3.Connection,
    trade_date: str,
    event_source: str | None = None,
) -> list[dict[str, Any]]:
    sql = 'SELECT * FROM stop_settlements WHERE trade_date=?'
    params: list[Any] = [trade_date]
    if event_source:
        sql += ' AND event_source=?'
        params.append(event_source)
    sql += ' ORDER BY signal_id, event_source'
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def load_pending_correction_anchors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''
            SELECT ca.signal_id, ca.symbol, ca.trade_date, ca.qualifier,
                   ca.zone_low, ca.zone_high, s.strategy_id
            FROM correction_anchors ca
            JOIN signals s ON s.id = ca.signal_id
            WHERE ca.noticed_at IS NULL
            ORDER BY ca.trade_date, ca.signal_id
            '''
        ).fetchall()
    ]


def mark_correction_notice(
    conn: sqlite3.Connection,
    signal_id: str,
    price: float,
    noticed_at: datetime,
) -> bool:
    cursor = conn.execute(
        '''
        UPDATE correction_anchors
        SET notice_price=?, noticed_at=?
        WHERE signal_id=? AND noticed_at IS NULL
        ''',
        (
            float(price), noticed_at.isoformat(timespec='seconds'), signal_id,
        ),
    )
    return cursor.rowcount > 0


def load_context_marks(
    conn: sqlite3.Connection,
    trade_date: str,
    status: ContextStatus | None = ContextStatus.ACTIVE,
) -> list[ContextMark]:
    sql = 'SELECT * FROM context_marks WHERE date=?'
    params: list[object] = [trade_date]
    if status is not None:
        sql += ' AND status=?'
        params.append(status.value)
    return [_context_mark_from_row(row) for row in conn.execute(sql, params).fetchall()]


def touch_context_mark(
    conn: sqlite3.Connection,
    trade_date: str,
    context_type: ContextType,
    code: str,
    triggered_at: datetime,
) -> bool:
    cursor = conn.execute(
        'UPDATE context_marks SET triggered_at=?, status=? WHERE date=? AND context_type=? AND code=?',
        (
            triggered_at.isoformat(timespec='seconds'), ContextStatus.ACTIVE.value,
            trade_date, context_type.value, code,
        ),
    )
    return cursor.rowcount > 0


def _context_mark_from_row(row: sqlite3.Row) -> ContextMark:
    return ContextMark(
        date=date.fromisoformat(row['date']),
        context_type=ContextType(row['context_type']),
        code=row['code'],
        level_low=row['level_low'],
        level_high=row['level_high'],
        label=row['label'],
        status=ContextStatus(row['status']),
        data_status=str(row['data_status'] or 'OK') if 'data_status' in row.keys() else 'OK',
        triggered_at=datetime.fromisoformat(row['triggered_at']) if row['triggered_at'] else None,
        created_at=datetime.fromisoformat(row['created_at']),
    )
