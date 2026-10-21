# -*- coding: utf-8 -*-
from src.plan import point_overlay


def _row(action, source, low, high, symbol="600001"):
    return {
        "symbol": symbol,
        "source": source,
        "zone_low": low,
        "zone_high": high,
        "action": action,
    }


def test_buy_matches_add_zone(monkeypatch):
    rows = [_row("ADD", "PRIOR_LOW", 98.0, 99.0)]
    monkeypatch.setattr(point_overlay, "list_point_entries", lambda _: rows)
    entries = [{
        "stock_code": "600001", "trigger_price": 98.6, "note": "价量突破",
    }]
    exits = []

    result = point_overlay.apply_point_table_overlay(entries, exits, "2026-10-21")

    assert entries[0]["point_status"] == "MATCH"
    assert "点表一致" in entries[0]["note"]
    assert result["matched"] == 1
    assert result["conflicts"] == 0


def test_buy_conflicts_with_sell_zone(monkeypatch):
    rows = [_row("STOP_DELAY", "MA5_TOMORROW", 100.0, 101.0)]
    monkeypatch.setattr(point_overlay, "list_point_entries", lambda _: rows)
    entries = [{
        "stock_code": "600001", "trigger_price": 100.4, "note": "确认追强",
    }]
    exits = []

    result = point_overlay.apply_point_table_overlay(entries, exits, "2026-10-21")

    assert entries[0]["point_status"] == "CONFLICT"
    assert "买入撞减仓/破位区" in entries[0]["note"]
    assert result["conflicts"] == 1


def test_sell_matches_stop_zone_and_conflicts_with_add_zone(monkeypatch):
    rows = [
        _row("STOP_DELAY", "MA5_TOMORROW", 100.0, 101.0),
        _row("ADD", "PRIOR_LOW", 98.0, 99.0),
    ]
    monkeypatch.setattr(point_overlay, "list_point_entries", lambda _: rows)
    exits = [{
        "stock_code": "600001", "trigger_price": 100.3,
        "reason": "MA5压制",
    }]

    result = point_overlay.apply_point_table_overlay([], exits, "2026-10-21")

    assert exits[0]["point_status"] == "MATCH"
    assert "卖出/减仓区一致" in exits[0]["reason"]
    assert result["matched"] == 1


def test_sector_state_change_marks_point_table_stale(monkeypatch):
    rows = [{
        "symbol": "600001",
        "source": "MA5_TOMORROW",
        "zone_low": 100.0,
        "zone_high": 101.0,
        "action": "ADD",
        "sector_state": "main_trend",
        "rotation_direction": "rising",
    }]
    monkeypatch.setattr(point_overlay, "list_point_entries", lambda _: rows)
    entries = [{
        "stock_code": "600001",
        "trigger_price": 100.4,
        "note": "价量突破",
        "sector_status": "retreating",
    }]

    result = point_overlay.apply_point_table_overlay(entries, [], "2026-10-21")

    assert entries[0]["point_status"] == "STALE"
    assert "SCENARIO_STALE" in entries[0]["note"]
    assert result["stale"] == 1
    assert result["matched"] == 0


def test_same_state_but_direction_change_is_stale(monkeypatch):
    rows = [{
        "symbol": "600001",
        "source": "MA5_TOMORROW",
        "zone_low": 100.0,
        "zone_high": 101.0,
        "action": "ADD",
        "sector_state": "main_trend",
        "rotation_direction": "rising",
        "market_mode": "defend",
    }]
    monkeypatch.setattr(point_overlay, "list_point_entries", lambda _: rows)
    entries = [{
        "stock_code": "600001",
        "trigger_price": 100.4,
        "note": "价量突破",
        "sector_status": "main_trend",
        "sector_name": "半导体",
        "sector_change_pct": -1.2,
    }]

    result = point_overlay.apply_point_table_overlay(entries, [], "2026-10-21")

    assert entries[0]["point_status"] == "STALE"
    assert result["stale"] == 1
