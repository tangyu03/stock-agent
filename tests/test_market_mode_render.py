# -*- coding: utf-8 -*-
from src.loop.market_mode_adaptive import MarketModeAdaptive
from src.analyzers.market_resonance import assess_sector_resonance
from src.push.templates import render_environment_overview


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


def test_sector_resonance_discloses_bullish_divergence_without_voting():
    sectors = [
        {"classification": "main_trend"},
        {"classification": "main_trend"},
        {"classification": "rotational"},
        {"classification": "retreating"},
        {"classification": "unknown"},
    ]

    result = assess_sector_resonance(
        "defend",
        sectors,
        snapshot_date="2026-10-16",
    )

    assert result["status"] == "bullish_divergence"
    assert result["counts"]["main_trend"] == 2
    assert result["counts"]["retreating"] == 1
    assert "仅观测不投票" in result["line"]


def test_environment_overview_omits_stale_sector_snapshot_warning():
    content = render_environment_overview({
        "market_mode": "defend",
        "sectors": {"main_trend": ["半导体"], "rotational": [], "retreating": []},
        "sector_snapshot_risk": {
            "available": True,
            "snapshot_date": "2026-08-22",
            "lag_days": 55,
            "line": "",
        },
    })

    assert "板块归属快照" not in content
    assert "板块状态标签仅供参考" not in content


def test_environment_overview_renders_sector_resonance():
    content = render_environment_overview({
        "market_mode": "defend",
        "sectors": {"main_trend": ["半导体"], "rotational": [], "retreating": []},
        "sector_resonance": {
            "line": "共振观测: 指数defend | 行业主线2(40%) | 退潮1(20%) | bullish_divergence | 快照2026-10-16,仅观测不投票",
        },
    })

    assert "共振 共振观测: 指数defend" in content
    assert "仅观测不投票" in content
