# -*- coding: utf-8 -*-
from src.plan.sector_playbook import (
    build_scenario,
    derive_rotation_direction,
    ma5_rule,
    resistance_rule,
    support_rule,
)


def test_main_trend_rising_uses_ma5_pullback_as_add():
    scenario = build_scenario(
        "defend", "main_trend", "半导体", 1.8, 2, 50, stock_change_pct=2.2
    )
    rule = ma5_rule(scenario, above_ma5=True, held=True)

    assert scenario["rotation_direction"] == "rising"
    assert scenario["scenario_id"] == "main_trend_rising"
    assert rule["action"] == "ADD"
    assert "放量跌破MA10" in rule["invalid_if"]


def test_retreating_sector_turns_ma5_into_stop():
    scenario = build_scenario(
        "retreat", "retreating", "消费电子", -1.8, 48, 50, stock_change_pct=-2.0
    )
    rule = ma5_rule(scenario, above_ma5=False, held=True)

    assert scenario["rotation_direction"] == "fading"
    assert rule["action"] == "STOP_DELAY"
    assert rule["action_size"] == 0.75


def test_support_is_not_a_low_absorption_setup_in_retreating_sector():
    scenario = build_scenario(
        "retreat", "retreating", "消费电子", -1.8, 48, 50
    )
    rule = support_rule(scenario, held=True)

    assert rule["action"] == "WATCH"
    assert "不支持低吸" in rule["invalid_if"]


def test_resistance_is_more_urgent_in_retreating_sector():
    scenario = build_scenario(
        "retreat", "retreating", "消费电子", -1.8, 48, 50
    )
    rule = resistance_rule(scenario, held=True)

    assert rule["action"] == "SELL_PART"
    assert rule["action_size"] == 0.75


def test_rotation_direction_covers_regimes():
    assert derive_rotation_direction("main_trend", -1.0) == "fading"
    assert derive_rotation_direction("rotational", 1.4) == "rising"
    assert derive_rotation_direction("rotational", -0.8) == "fading"
    assert derive_rotation_direction("retreating", 0.8) == "stabilizing"

