from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..models.receipt import ReceiptVerdict
from ..models.signal import Signal, SignalStatus, SignalTag


@dataclass(frozen=True)
class UnpairedT:
    signal_id: str
    symbol: str
    parent_id: str
    trade_date: str


def restore_reminders(signals: Iterable[Signal], close_price: float) -> list[Signal]:
    '''A same-day recovery is a reminder only; it never rewrites the stop state.'''
    return [
        signal
        for signal in signals
        if (
            signal.tag is SignalTag.STOP
            and signal.status is SignalStatus.TRIGGERED
            and signal.zone_low <= close_price <= signal.zone_high
        )
    ]



def _signal_trade_date(signal: Signal) -> str:
    first_part = signal.id.split('-', 1)[0]
    if len(first_part) == 10 and first_part[4:5] == '-' and first_part[7:8] == '-':
        return first_part
    return signal.created_at.isoformat(timespec='seconds')[:10]


def find_unpaired_t_buy(
    signals: Iterable[Signal],
    receipts: Iterable[Mapping[str, object]],
    trade_date: str | None = None,
) -> tuple[list[UnpairedT], list[str], list[str]]:
    rows = list(signals)
    t_rows = [row for row in rows if row.tag is SignalTag.T]
    if trade_date is not None:
        t_rows = [row for row in t_rows if _signal_trade_date(row) == trade_date]
    sells_by_parent = {
        row.parent_id: row
        for row in t_rows
        if row.side.value == 'SELL' and row.parent_id
    }
    executed_signal_ids = {
        str(row['signal_id'])
        for row in receipts
        if str(row.get('verdict') or '') == ReceiptVerdict.EXECUTED.value
    }
    unpaired: list[UnpairedT] = []
    no_receipt: list[str] = []
    violations: list[str] = []
    for signal in t_rows:
        if signal.side.value != 'BUY' or not signal.parent_id:
            continue
        if signal.id not in executed_signal_ids:
            no_receipt.append(signal.id)
            continue
        paired_sell = sells_by_parent.get(signal.parent_id)
        if paired_sell and paired_sell.symbol != signal.symbol:
            paired_sell = None
        if paired_sell and paired_sell.id in executed_signal_ids:
            continue
        unpaired.append(UnpairedT(
            signal_id=signal.id,
            symbol=signal.symbol,
            parent_id=signal.parent_id,
            trade_date=_signal_trade_date(signal),
        ))
        violations.append(signal.id)
    return unpaired, no_receipt, violations
