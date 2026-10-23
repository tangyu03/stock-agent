from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any


def today_execution_review(conn: sqlite3.Connection, trade_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT s.id, s.symbol, s.strategy_id, s.tag, s.side, s.status,
               s.zone_low, s.zone_high, j.created_at AS triggered_at
        FROM journal j
        JOIN signals s ON s.id = j.signal_id
        WHERE j.to_status='TRIGGERED' AND j.created_at LIKE ?
        ORDER BY j.created_at, s.id
        ''',
        (trade_date + '%',),
    ).fetchall()

    receipts: dict[str, str] = {}
    for row in conn.execute(
        'SELECT signal_id, verdict FROM receipts ORDER BY created_at, id'
    ).fetchall():
        receipts[str(row['signal_id'])] = str(row['verdict'])

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        verdict = receipts.get(str(row['id']))
        mechanical = str(row['side']) == 'SELL'
        if verdict == 'executed':
            difference = ''
        elif verdict == 'ignored':
            difference = '机械信号未执行：ignored'
        elif verdict == 'veto':
            difference = '人工否决机械信号：veto'
        elif verdict is None:
            difference = '未回执' if mechanical else '待回执'
        else:
            difference = '回执状态未知'
        reviewed.append({
            'signal_id': str(row['id']),
            'symbol': str(row['symbol']),
            'strategy_id': str(row['strategy_id']),
            'tag': str(row['tag']),
            'zone_low': row['zone_low'],
            'zone_high': row['zone_high'],
            'triggered_at': str(row['triggered_at']),
            'mechanical': mechanical,
            'verdict': verdict,
            'difference': difference,
        })
    return reviewed
