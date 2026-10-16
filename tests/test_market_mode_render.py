# -*- coding: utf-8 -*-
from src.loop.market_mode_adaptive import MarketModeAdaptive


def _mma():
    mma = MarketModeAdaptive()
    mma.get_gem_sci_tech_analysis = lambda: {"trend_judgment": "测试中性"}
    mma._count_distribution_days = lambda *_a, **_k: {
        "count": 0, "window": 25, "stale": False,
    }
    return mma


def _kline():
    return [
        {"close": 100.0, "volume": 1000, "date": f"2024-01-{day:02d}"}
        for day in range(1, 61)
    ]


def test_index_trend_discloses_full_numeric_precision(monkeypatch):
    mma = _mma()
    monkeypatch.setattr(
        mma, "_get_advance_decline",
        lambda: {"available": True, "advance": 4411, "decline": 757,
                 "flat": 100, "ratio": 5.83},
    )
    result = mma._assess_market("2024-02-29", _kline())

    condition = result["dimensions"][0]["condition"]
    assert "收盘100.00" in condition
    assert "MA20(收盘100.00" in condition
    assert "MA50(收盘100.00" in condition


def test_defend_mode_marks_extreme_breadth_for_observation_upgrade(monkeypatch):
    mma = _mma()
    monkeypatch.setattr(
        mma, "_get_advance_decline",
        lambda: {"available": True, "advance": 4411, "decline": 757,
                 "flat": 100, "ratio": 5.83},
    )
    result = mma._assess_market("2024-02-29", _kline())

    assert result["mode"] == "defend"
    assert result["breadth_upgrade"] is True
    assert "宽度5.83:1" in result["mode_reason"]
    assert "观察池升级1~2只" in result["mode_reason"]
