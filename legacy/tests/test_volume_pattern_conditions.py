from src.analyzers.volume_pattern import build_volume_pattern
from src.push.templates import render_action_summary


def _flow(pct):
    return {
        "available": True,
        "outer_volume": 520 if pct >= 0 else 440,
        "inner_volume": 480 if pct >= 0 else 560,
        "imbalance_pct": pct,
    }


def _base_data(**overrides):
    data = {
        "stock_code": "688008",
        "stock_name": "测试",
        "current_price": 95.0,
        "change_pct": 2.0,
        "volume_ratio": 1.4,
        "turnover_rate": 4.0,
        "ma5": 105.0,
        "ma10": 107.0,
        "ma20": 110.0,
        "prior_high": 120.0,
        "recent_high": 121.0,
        "gain_20d": 0.10,
        "ma20_falling": True,
        "tech_signals": {"rsi6": 40.0, "order_flow": _flow(2.0)},
    }
    data.update(overrides)
    return data


def test_healthy_push_below_ma5_uses_recovery_condition():
    result = build_volume_pattern(_base_data())
    assert result["pattern"] == "健康推进型"
    assert any("收复MA5后再复核" in item for item in result["confirms"])
    assert any("未收复MA5前不做追强" in item for item in result["confirms"])


def test_real_pressure_below_ma20_uses_risk_condition():
    data = _base_data(
        current_price=95.0,
        change_pct=-4.0,
        volume_ratio=0.8,
        tech_signals={"rsi6": 40.0, "order_flow": _flow(-12.0)},
    )
    result = build_volume_pattern(data)
    assert result["pattern"] == "真实抛压型"
    assert any("不收复MA20则风控优先" in item for item in result["confirms"])


def test_action_summary_labels_bearish_conditions_as_risk():
    data = _base_data(
        current_price=95.0,
        change_pct=-4.0,
        volume_ratio=0.8,
        tech_signals={"rsi6": 40.0, "order_flow": _flow(-12.0)},
    )
    rendered = render_action_summary([], [], [data])
    assert "待验(风控):" in rendered
