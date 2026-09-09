from types import SimpleNamespace


class _AdaptiveEnv:
    def assess_daily(self, force_refresh=False, ref_date=None):
        return {
            "market_mode": "defend",
            "market_score": 5.0,
            "position_limit": 0.5,
        }


class _TimingEngine:
    _tech_data_full = {}
    _exit_diagnostics = {}


def test_low_confidence_signal_is_scheduled_not_observed(monkeypatch):
    from src.orchestrator import engine as engine_module
    from src.orchestrator import unified_engine
    from src.loop import market_mode_adaptive
    from src.analyzers import timing_engine
    from src.decision import live_scheduler

    raw_signal = SimpleNamespace(
        stock_code="688028",
        stock_name="WALD",
        entry_type="price_volume_breakout",
        entry_trigger_price=102.09,
        stop_loss=95.36,
        target_range=[110.0],
        position_level="normal",
        sector_status="main_trend",
        sector_name="general_equipment",
        sw_level2="",
        trigger_reason="raw entry",
        confidence="low",
        benchmark_price=100.0,
        rrr_low=2.0,
        rrr_high=3.0,
        execution_plan={"execute": False, "confidence_details": ["low confidence"]},
        tech_data={"current_price": 102.09},
    )
    batch = SimpleNamespace(
        entries=[raw_signal],
        exits=[],
        stock_sector={},
        stock_sector_status={},
        entry_diagnostics={},
    )

    monkeypatch.setattr(engine_module, "_resolve_run_context", lambda: (True, "", "2026-09-03"))
    monkeypatch.setattr(engine_module, "load_config", lambda name: {"stocks": [{"code": "688028"}]})
    monkeypatch.setattr(market_mode_adaptive, "get_market_mode_adaptive", lambda: _AdaptiveEnv())
    monkeypatch.setattr(unified_engine, "run_unified_analysis", lambda **kwargs: batch)
    monkeypatch.setattr(timing_engine, "get_timing_engine", lambda: _TimingEngine())

    scheduled_entries = []

    def _capture_schedule(entry_signals, exit_signals, **kwargs):
        scheduled_entries.extend(entry_signals)
        return {
            "buy": [],
            "sell": [],
            "skipped": {},
            "stats": {"buy_executed": 0, "sell_executed": 0, "entry_in": len(entry_signals), "sell_in": 0},
        }

    scheduled = {
        "buy": [],
        "sell": [],
        "skipped": {},
        "stats": {"buy_executed": 0, "sell_executed": 0, "entry_in": 0, "sell_in": 0},
    }
    monkeypatch.setattr(live_scheduler, "schedule_live_signals", _capture_schedule)
    monkeypatch.setattr(live_scheduler, "format_scheduled_summary", lambda scheduled: "")

    sent_observations = []

    def _capture_report(env, buys, sells, observations):
        sent_observations.extend(observations)

    orchestrator = SimpleNamespace(
        _pushplus=SimpleNamespace(send_intraday_report=_capture_report),
        _trade_logger=SimpleNamespace(get_pending_signals=lambda day: []),
    )

    engine_module.Orchestrator._do_intraday(
        orchestrator,
        force=True,
    )

    assert [item["stock_code"] for item in scheduled_entries] == ["688028"]
    assert scheduled_entries[0]["confidence"] == "low"
    assert sent_observations == []


def test_missing_price_stock_is_reported_not_observed(monkeypatch):
    from src.orchestrator import engine as engine_module
    from src.orchestrator import unified_engine
    from src.loop import market_mode_adaptive
    from src.analyzers import timing_engine
    from src.decision import live_scheduler

    raw_signal = SimpleNamespace(
        stock_code="688028",
        stock_name="WALD",
        entry_type="price_volume_breakout",
        entry_trigger_price=102.09,
        stop_loss=95.36,
        target_range=[110.0],
        position_level="normal",
        sector_status="main_trend",
        sector_name="general_equipment",
        sw_level2="",
        trigger_reason="raw entry",
        confidence="low",
        benchmark_price=100.0,
        rrr_low=2.0,
        rrr_high=3.0,
        execution_plan={"execute": False, "confidence_details": ["low confidence"]},
        tech_data={"current_price": 102.09},
    )
    batch = SimpleNamespace(
        entries=[raw_signal],
        exits=[],
        stock_sector={},
        stock_sector_status={},
        entry_diagnostics={},
    )

    class _MissingPriceTimingEngine:
        _tech_data_full = {"301666": {}}
        _exit_diagnostics = {
            "301666": "卖出检查: 现价缺失，数据不足，未评估",
        }

    monkeypatch.setattr(engine_module, "_resolve_run_context", lambda: (True, "", "2026-09-03"))
    monkeypatch.setattr(
        engine_module,
        "load_config",
        lambda name: {
            "stocks": [
                {"code": "688028", "name": "WALD"},
                {"code": "301666", "name": "DAPU"},
            ]
        },
    )
    monkeypatch.setattr(market_mode_adaptive, "get_market_mode_adaptive", lambda: _AdaptiveEnv())
    monkeypatch.setattr(unified_engine, "run_unified_analysis", lambda **kwargs: batch)
    monkeypatch.setattr(timing_engine, "get_timing_engine", lambda: _MissingPriceTimingEngine())

    scheduled = {
        "buy": [],
        "sell": [],
        "skipped": {},
        "stats": {"buy_executed": 0, "sell_executed": 0, "entry_in": 0, "sell_in": 0},
    }
    monkeypatch.setattr(
        live_scheduler,
        "schedule_live_signals",
        lambda entry_signals, exit_signals, **kwargs: scheduled,
    )
    monkeypatch.setattr(live_scheduler, "format_scheduled_summary", lambda scheduled: "")

    sent_env = {}
    sent_observations = []

    def _capture_report(env, buys, sells, observations):
        sent_env.update(env)
        sent_observations.extend(observations)

    orchestrator = SimpleNamespace(
        _pushplus=SimpleNamespace(send_intraday_report=_capture_report),
        _trade_logger=SimpleNamespace(
            get_pending_signals=lambda day: [],
            get_current_holdings=lambda: [{"code": "301666", "name": "DAPU"}],
        ),
    )

    engine_module.Orchestrator._do_intraday(orchestrator, force=True)

    assert sent_observations == []
    assert sent_env["data_missing"] == [
        {
            "stock_name": "DAPU",
            "stock_code": "301666",
            "actual_holding": True,
            "reason": "卖出检查: 现价缺失，数据不足，未评估",
        }
    ]


def test_missing_price_report_has_explicit_section(monkeypatch):
    from src.push.pushplus import PushPlus

    captured = {}

    def _send(self, title, content, level="常规"):
        captured.update({"title": title, "content": content, "level": level})
        return True

    monkeypatch.setattr(PushPlus, "send", _send)
    PushPlus.__new__(PushPlus).send_intraday_report(
        {
            "market_mode": "defend",
            "market_score": 5.0,
            "data_missing": [{
                "stock_name": "DAPU",
                "stock_code": "301666",
                "actual_holding": True,
                "reason": "现价未取到，卖出/观察判定未执行",
            }],
        }
    )

    assert "数据缺失1" in captured["title"]
    assert "数据未取到 (1条)" in captured["content"]
    assert "DAPU(301666)" in captured["content"]
    assert "现价未取到" in captured["content"]
    assert captured["level"] == "重要"
