from types import SimpleNamespace


def _kline(price=100.0):
    return [
        {
            "close": price,
            "open": price - 0.1,
            "high": price + 0.1,
            "low": price - 0.5,
            "volume": 1000,
        }
        for _ in range(25)
    ]


def test_ma20_rising_prefers_explicit_previous_value():
    from src.analyzers.timing_utils import ma20_rising

    assert ma20_rising({"ma20": 101.0, "ma20_prev": 100.0}) is True
    assert ma20_rising({"ma20": 100.0, "ma20_prev": 101.0}) is False


def test_ma20_rising_fails_closed_without_evidence():
    from src.analyzers.timing_utils import ma20_rising

    assert ma20_rising({"kline": [{"close": 100}]}) is False


def test_panic_bottom_blocks_when_ma20_falls(monkeypatch):
    import src.analyzers.timing_entry_checkers as entry_mod
    from src.analyzers.timing_engine import get_backtest_timing_engine

    monkeypatch.setattr(
        entry_mod,
        "get_market_environment",
        lambda *args, **kwargs: {"volume_percentile": 50.0},
    )
    tech = {
        "current_price": 100.0,
        "change_pct": -8.0,
        "drop_5d": -0.20,
        "index_daily_drop": -5.0,
        "index_cum_drop_5d": -7.0,
        "index_down_days_5d": 4,
        "advance_decline_ratio": 0.10,
        "tech_signals": {"rsi": 20.0},
        "kline": _kline(),
        "ma20_falling": True,
    }
    engine = get_backtest_timing_engine()
    signal = engine._check_panic_bottom(
        "688008", "测试", tech, SimpleNamespace(stop_loss_price=90.0), "defend", "rotational",
    )
    assert signal is None


def test_ma5_pressure_signal_contains_ma10_buyback(monkeypatch):
    from src.analyzers.timing_engine import TimingEngine

    tech = {
        "current_price": 99.0,
        "ma5": 102.0,
        "ma10": 101.0,
        "ma20": 100.0,
        "ma5_prev": 101.8,
        "ma5_prev2": 101.6,
        "volume_ratio": 1.5,
        "kline": _kline(),
        "tech_signals": {},
    }
    engine = TimingEngine(backtest_mode=False)
    monkeypatch.setattr(engine, "_fetch_tech_data", lambda code, mode="defend": tech)
    signals = engine.check_exit_signals("688028", "测试", "defend")
    signal = next(item for item in signals if item.exit_type == "MA5压制")
    assert "接回参考MA10=101.00" in signal.reason


def _t0_trend_up():
    kline = _kline(99.8)
    kline[21]["low"] = 99.0
    kline[20]["high"] = 101.0
    return {
        "current_price": 99.8,
        "ma5": 100.5,
        "ma10": 100.2,
        "ma20": 100.0,
        "ma20_prev": 99.7,
        "volume_ratio": 0.9,
        "kline": kline,
    }


def test_t0_adds_on_supported_uptrend_pullback():
    from src.analyzers.timing_t0 import build_t0_signal

    holding = {"code": "688008", "stock_name": "测试", "shares": 1000, "cost_price": 105.0}
    signal = build_t0_signal(holding, _t0_trend_up(), "defend")
    assert signal["signal_type"] == "正T-加仓低吸"
    assert signal["direction"] == "正T"
    assert signal["t0_shares"] == 200
    assert signal["stop_loss_price"] == 98.5


def test_t0_downcycle_does_not_buy_support():
    from src.analyzers.timing_t0 import build_t0_signal

    tech = _t0_trend_up()
    tech.update({"ma5": 99.0, "ma10": 100.0, "ma20": 101.0, "ma20_falling": True})
    tech["kline"][20]["high"] = 100.0
    tech["current_price"] = 97.5
    holding = {"code": "688008", "stock_name": "测试", "shares": 1000, "cost_price": 105.0}
    assert build_t0_signal(holding, tech, "defend") is None


def test_t0_reverse_half_near_resistance():
    from src.analyzers.timing_t0 import build_t0_signal

    tech = _t0_trend_up()
    tech.update({
        "current_price": 100.8,
        "ma5": 99.0,
        "ma10": 100.0,
        "ma20": 101.0,
        "ma20_falling": True,
        "volume_ratio": 1.2,
        "gain_20d": 0.10,
    })
    holding = {"code": "688008", "stock_name": "测试", "shares": 1000, "cost_price": 90.0}
    signal = build_t0_signal(holding, tech, "defend")
    assert signal["signal_type"] == "反T-半仓高抛"
    assert signal["t0_shares"] == 500


def test_t0_reverse_full_on_extended_heavy_volume():
    from src.analyzers.timing_t0 import build_t0_signal

    tech = _t0_trend_up()
    tech.update({
        "current_price": 100.8,
        "volume_ratio": 1.6,
        "gain_20d": 0.40,
    })
    tech["kline"][21]["low"] = 98.0
    holding = {"code": "688008", "stock_name": "测试", "shares": 1000, "cost_price": 90.0}
    signal = build_t0_signal(holding, tech, "defend")
    assert signal["signal_type"] == "反T-全仓高抛"
    assert signal["t0_shares"] == 1000


def _break_tech(**overrides):
    tech = {
        "current_price": 82.0,
        "ma5": 81.0,
        "ma10": 88.0,
        "ma20": 95.0,
        "change_pct": -3.0,
        "volume_ratio": 0.5,
        "kline": _kline(82.0),
        "tech_signals": {},
    }
    tech.update(overrides)
    return tech


def test_breakdown_with_complete_sector_data_labels_active(monkeypatch):
    from src.analyzers.timing_engine import TimingEngine

    tech = _break_tech(change_pct=-3.0, sector_change_pct=-2.0)
    engine = TimingEngine(backtest_mode=False)
    monkeypatch.setattr(engine, "_fetch_tech_data", lambda code, mode="defend": tech)
    engine.calculate_stop_loss = lambda code, data=None: SimpleNamespace(
        stop_loss_price=84.0, chosen_support=88.0,
    )

    signals = engine.check_exit_signals("688028", "测试", "defend", sector_change_pct=-2.0)
    breakdown = next(item for item in signals if "破位" in item.exit_type)
    assert "缩量+主动" in breakdown.reason


def test_breakdown_without_sector_data_stays_unknown(monkeypatch):
    from src.analyzers.timing_engine import TimingEngine

    tech = _break_tech(change_pct=-3.0)
    engine = TimingEngine(backtest_mode=False)
    monkeypatch.setattr(engine, "_fetch_tech_data", lambda code, mode="defend": tech)
    engine.calculate_stop_loss = lambda code, data=None: SimpleNamespace(
        stop_loss_price=84.0, chosen_support=88.0,
    )

    signals = engine.check_exit_signals("688028", "测试", "defend")
    breakdown = next(item for item in signals if "破位" in item.exit_type)
    assert "缩量+口径未知" in breakdown.reason


def test_today_t0_signal_keys_include_both_legs(monkeypatch, tmp_path):
    import src.db as db_mod
    from src.feedback.trade_logger import TradeLogger

    db_path = tmp_path / "trade_logs.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    logger = TradeLogger()
    logger.log_signal(
        "t0_buy", "688008", "测试", {"entry_type": "正T", "trigger_price": 100.0}
    )
    logger.log_signal(
        "t0_sell", "688008", "测试", {"entry_type": "正T", "trigger_price": 101.0}
    )

    keys = logger.get_today_t0_signal_keys()
    assert ("688008", "t0_buy", "正T", 100.0) in keys
    assert ("688008", "t0_sell", "正T", 101.0) in keys
    db_mod.close_thread_connection()
