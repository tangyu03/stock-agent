# -*- coding: utf-8 -*-
from src.plan.point_table import (
    _add_entry,
    calculate_tomorrow_ma_zone,
    render_shadow_point_table,
)


def test_tomorrow_ma_uses_low_flat_high_scenarios():
    closes = [10.0] * 5
    zone = calculate_tomorrow_ma_zone(closes, period=5, scenario_ratio=0.02)

    assert abs(zone["low"] - 9.96) < 1e-9
    assert abs(zone["mid"] - 10.0) < 1e-9
    assert abs(zone["high"] - 10.04) < 1e-9


def test_tomorrow_ma_requires_enough_history():
    assert calculate_tomorrow_ma_zone([10.0, 11.0], period=5) is None


def test_invalid_zone_is_rejected():
    entries = []
    _add_entry(
        entries, "2026-10-20", "2026-10-21", "600001", "测试",
        held=True, source="MA5_TOMORROW",
        zone={"low": 11.0, "high": 10.0, "mid": 10.5},
        action="STOP_DELAY", action_size=0.5,
        invalid_if="放量站回MA5",
    )
    assert entries == []


def test_shadow_table_reports_non_dispatch_state():
    entries = [{
        "symbol": "600001",
        "stock_name": "测试股",
        "mode": "POINT",
        "trade_date": "2026-10-21",
        "source": "MA5_TOMORROW",
        "zone_low": 99.8,
        "zone_high": 100.2,
        "action": "SELL",
        "action_size": 1.0,
        "verify_volume": "ANY",
        "reverse_action": "BUY",
        "invalid_if": "BUY:跌破区间转SELL；SELL:站回区间转BUY",
    }]
    text = render_shadow_point_table(entries)

    assert "影子运行" in text
    assert "未签字、不触发、不参与调度" in text
    assert "99.80-100.20" in text
    assert "反向" in text
