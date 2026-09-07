from src.data_layer.stock_data import calc_tech_indicators


def _rising_kline_with_volume():
    kline = []
    price = 50.0
    for index in range(61):
        if index < 30:
            price -= 0.5
        else:
            price += 1.0
        kline.append({
            "open": price - 0.1,
            "high": price + 0.2,
            "low": price - 0.2,
            "close": price,
            "volume": 5000 if index == 60 else 1000,
            "turnover_rate": 20.0 if index == 60 else 1.0,
        })
    return kline


def test_technical_votes_only_use_finalized_indicators():
    tech = calc_tech_indicators(_rising_kline_with_volume())

    vote_details = " | ".join(tech["vote_details"])
    trend_details = tech["category_votes"]["trend"]["details"]
    momentum_details = tech["category_votes"]["momentum"]["details"]

    assert "EMA金叉" not in vote_details
    assert "EMA死叉" not in vote_details
    assert not any("ADX" in detail for detail in trend_details)
    assert not any("背驰" in detail for detail in trend_details)
    assert not any("KDJ" in detail for detail in momentum_details)
    assert not any("布林" in detail for detail in momentum_details)
    assert any("RSI6=" in detail for detail in momentum_details)
    assert tech["category_votes"]["volume"]["vote"] == 1
    assert any("放量上涨" in detail for detail in tech["category_votes"]["volume"]["details"])
    assert tech["volume_snapshot"]["turnover_hot"] is True


def test_bias6_and_order_flow_are_display_only():
    kline = [{
        "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
        "volume": 1000, "turnover_rate": 1.0,
    } for _ in range(30)]

    tech = calc_tech_indicators(
        kline,
        realtime_quote={"outer_volume": 700, "inner_volume": 300},
    )

    assert "BIAS6=0.0%(展示)" in tech["category_votes"]["momentum"]["details"]
    assert tech["category_votes"]["momentum"]["vote"] == 0
    assert tech["order_flow"]["outer_volume"] == 700
    assert tech["order_flow"]["inner_volume"] == 300
    assert "外盘700手/内盘300手(外盘占优,展示)" not in tech["category_votes"]["volume"]["details"]
    assert tech["order_flow"]["display_only"] is True
