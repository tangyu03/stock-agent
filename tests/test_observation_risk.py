def test_observation_technical_stop_is_concrete_or_explicit_none():
    from src.orchestrator.engine import _observation_technical_stop
    from src.analyzers.timing_models import StopLossCalc

    code = "688008"
    tech = {
        "current_price": 100.0,
        "change_pct": 0.0,
        "ma5": 101.0,
        "ma10": 102.0,
        "ma20": 103.0,
        "volume_ratio": 1.0,
        "kline": [],
    }

    class Engine:
        _tech_data_full = {code: tech}
        _exit_diagnostics = {code: "无卖出信号"}

        def calculate_stop_loss(self, stock_code, tech_data=None):
            return StopLossCalc(
                stock_code=stock_code,
                current_price=100.0,
                support_candidates=[],
                chosen_support=98.0,
                ladder=[],
                stop_loss_price=98.0,
                resistance=105.0,
            )

    engine = Engine()
    stop = engine.calculate_stop_loss(code, tech)
    assert stop.stop_loss_price == 98.0
    assert _observation_technical_stop(engine, code, tech) == 98.0

    class BrokenEngine:
        def calculate_stop_loss(self, stock_code, tech_data=None):
            raise RuntimeError("data missing")

    assert _observation_technical_stop(BrokenEngine(), code, tech) is None
