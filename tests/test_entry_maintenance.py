from datetime import date

from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore,
    SignalLifecycle,
)


def test_entry_snapshot_and_maintenance_check_render_separately():
    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
        hypothesis={"x": "突破MA25", "y": 102.77, "z": 85.54},
        born=date(2026, 9, 8),
        entry_snapshot={
            "volume_ratio": 1.3,
            "ma_alignment": True,
            "rsi": 61.0,
            "sector_status": "主线",
        },
    )

    lifecycle.set_maintenance_check(
        "002975",
        {
            "date": "2026-09-09",
            "volume_ratio": 0.8,
            "ma_alignment": True,
            "rsi": 57.0,
            "sector_status": "轮动",
        },
    )

    event = lifecycle.get_active_events("002975")[0]
    note = lifecycle.event_status_note("002975", current_price=103.5)

    assert "入场(T0 2026-09-08): 量比1.30✓ 多头排列✓ RSI61.0 板块主线" in note
    assert "维持(今日): 量比0.80 △缩量 多头排列✓ RSI57.0 板块轮动" in note
    assert "维持条件走弱，不影响已触发状态，仅提示" in note
    assert event.status == "valid"
    assert event.entry_snapshot.count("1.3") == 1
