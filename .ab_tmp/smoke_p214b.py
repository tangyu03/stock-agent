import sys
sys.path.insert(0, r'C:\Users\15831\Downloads\stock-agent2')
from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore, SignalLifecycle,
)

lc2 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev2 = lc2.register_event(
    "300308", "中际旭创", "价量突破",
    breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
    target_low=907.0, target_high=915.0,
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

lc3 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev3 = lc3.register_event(
    "300308", "中际旭创", "价量突破",
    breakout_level=889.12, entry_price=890.0, stop_loss=821.99,
    target_low=907.0, target_high=915.0,
)
lc3.mark_filled(ev3.event_id)
lc3.mark_signal_target(ev3.event_id, trigger_data={
    "close": 908.09, "today": "2026-09-16",
})
note3 = lc3.event_status_note("300308", current_price=908.09)
print("CASE3:", note3)
assert "完结价908.09(收盘)" in note3, note3

# 旧测试兼容：无口径字段但 key=price → 现价快照
lc4 = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
ev4 = lc4.register_event(
    "002975", "博杰股份", "价量突破",
    breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
    target_low=112.89, target_high=118.0,
)
lc4.mark_filled(ev4.event_id)
lc4.mark_chase_abandon(ev4.event_id, trigger_data={
    "price": 583.2, "today": "2026-09-10",
})
note4 = lc4.event_status_note("002975", current_price=583.2)
print("CASE4:", note4)
assert "完结价583.20(现价快照)" in note4, note4
print("OK")
