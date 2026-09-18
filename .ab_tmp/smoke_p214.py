import sys
sys.path.insert(0, r'C:\Users\15831\Downloads\stock-agent2')
from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore, SignalLifecycle,
)
from datetime import date

lc = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev = lc.register_event(
    "300308", "中际旭创", "价量突破",
    breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
    target_low=907.0, target_high=915.0,
)
# 完结价 908.09 现价快照 vs 交易所收盘 907.80 → 双值
lc.mark_filled(ev.event_id)
lc.mark_signal_target(ev.event_id, trigger_data={
    "close": 908.09, "price_source": "现价快照", "exchange_close": 907.80,
    "today": "2026-09-16",
})
note = lc.event_status_note("300308", current_price=908.09)
print("CASE1:", note)
assert "完结价908.09(现价快照) vs 交易所收盘907.80" in note, note

# 完结价 == 交易所收盘 → 单值带口径
lc2 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev2 = lc2.register_event(
    "300308", "中际旭创", "价量突破",
    breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
)
lc2.mark_filled(ev2.event_id)
lc2.mark_signal_target(ev2.event_id, trigger_data={
    "close": 907.80, "price_source": "收盘", "exchange_close": 907.80,
    "today": "2026-09-16",
})
note2 = lc2.event_status_note("300308", current_price=907.80)
print("CASE2:", note2)
assert "完结价907.80(收盘)" in note2
assert " vs 交易所收盘" not in note2, note2

# 旧事件无口径字段 → 按 close 键名推断为收盘
lc3 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev3 = lc3.register_event(
    "300308", "中际旭创", "价量突破",
    breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
)
lc3.mark_filled(ev3.event_id)
lc3.mark_signal_target(ev3.event_id, trigger_data={
    "close": 908.09, "today": "2026-09-16",
})
note3 = lc3.event_status_note("300308", current_price=908.09)
print("CASE3:", note3)
assert "完结价908.09(收盘)" in note3, note3
print("OK")
