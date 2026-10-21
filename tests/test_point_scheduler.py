# -*- coding: utf-8 -*-
from src.decision.live_scheduler import (
    build_point_scheduled_signals,
    schedule_live_signals,
)
from src.plan.point_decision import decide_from_points, decide_rows_from_points


def _buy_row():
    return {
        "id": "buy-row",
        "stock_code": "600001",
        "action": "BUY",
        "source": "PRIOR_LOW_L1",
        "zone_low": 99.0,
        "zone_high": 100.0,
        "action_size": 0.5,
        "reverse_action": "SELL",
        "invalid_if": "跌破区间转SELL",
    }


def _sell_row():
    return {
        "id": "sell-row",
        "stock_code": "600001",
        "action": "STOP_DELAY",
        "source": "MA5_TOMORROW",
        "zone_low": 100.0,
        "zone_high": 101.0,
        "action_size": 0.3,
        "reverse_action": "BUY",
        "invalid_if": "站回区间转BUY",
    }


def test_legacy_point_actions_are_normalized():
    assert decide_from_points([_buy_row()], 99.5)["action"] == "BUY"
    assert decide_from_points([_sell_row()], 100.5)["action"] == "SELL"


def test_each_touched_point_keeps_its_own_action():
    decisions = decide_rows_from_points([_buy_row(), _sell_row()], 100.0)

    assert [item["action"] for item in decisions] == ["BUY", "SELL"]
    assert decisions[0]["point_id"] == "buy-row"
    assert decisions[1]["point_id"] == "sell-row"


def test_point_signals_are_dispatched_separately():
    result = build_point_scheduled_signals([
        {**_buy_row(), "stock_code": "600001", "stock_name": "测试"},
        {**_sell_row(), "stock_code": "600001", "stock_name": "测试"},
    ])

    assert len(result["point_buy"]) == 1
    assert len(result["point_sell"]) == 1
    buy = result["point_buy"][0]
    sell = result["point_sell"][0]
    assert buy.entry_type == "POINT:PRIOR_LOW_L1"
    assert sell.exit_type == "POINT:MA5_TOMORROW"
    assert buy.action_size == 0.5
    assert sell.action_size == 0.3
    assert "反向:SELL" in buy.schedule_note
    assert "失效:站回区间转BUY" in sell.schedule_note


def test_scheduler_returns_point_signals_without_mixing_strategy_signals():
    scheduled = schedule_live_signals(
        entry_signals=[],
        exit_signals=[],
        point_decisions=[{
            **_sell_row(),
            "stock_code": "600001",
            "stock_name": "测试",
        }],
    )

    assert scheduled["buy"] == []
    assert scheduled["sell"] == []
    assert len(scheduled["point_sell"]) == 1
    assert scheduled["stats"]["point_sell"] == 1
