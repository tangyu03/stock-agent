# -*- coding: utf-8 -*-
from src.plan.point_decision import decide_from_points


def _row(action, low, high, source="MA5_TOMORROW"):
    return {
        "action": action,
        "source": source,
        "zone_low": low,
        "zone_high": high,
    }


def test_price_in_buy_zone_returns_buy():
    decision = decide_from_points([_row("BUY", 99.8, 100.2)], 100.0)

    assert decision["action"] == "BUY"
    assert "99.80-100.20" in decision["reason"]


def test_price_in_sell_zone_returns_sell():
    decision = decide_from_points([_row("SELL", 110.0, 111.0)], 110.4)

    assert decision["action"] == "SELL"


def test_price_between_zones_watches():
    decision = decide_from_points([
        _row("BUY", 99.0, 100.0),
        _row("SELL", 110.0, 111.0),
    ], 105.0)

    assert decision["action"] == "WATCH"
    assert decision["reason"] == "未触及点位"


def test_nearest_zone_wins_when_overlapping():
    decision = decide_from_points([
        _row("SELL", 99.0, 100.0),
        _row("BUY", 100.0, 101.0),
    ], 100.02)

    assert decision["action"] == "BUY"

