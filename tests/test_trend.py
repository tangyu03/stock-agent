from pointtable.core.trend import classify_trend
from pointtable.models.signal import SignalTrend


def test_up_trend_support_is_a_buy_level():
    closes = [float(value) for value in range(1, 21)] + [21.0]
    assert classify_trend(closes) is SignalTrend.UP


def test_down_trend_same_level_is_a_sell_level():
    closes = [float(value) for value in range(25, 5, -1)]
    assert classify_trend(closes) is SignalTrend.DOWN


def test_insufficient_history_defaults_down():
    assert classify_trend([1.0, 2.0, 3.0]) is SignalTrend.DOWN
