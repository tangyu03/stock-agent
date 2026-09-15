from datetime import date
from pathlib import Path
import uuid

import pytest


def _base_tech(**overrides):
    tech = {
        "current_price": 530.77,
        "recent_high": 524.0,
        "prior_high": 524.0,
        "volume_ratio": 2.01,
        "adx": 48.0,
        "rsi": 64.7,
        "outer_volume": 862_000,
        "inner_volume": 632_000,
        "kline": [{
            "close": 500.0 + i,
            "volume": 1_000_000,
        } for i in range(30)],
        "fundamental": {"profit_yoy": 113.3, "profit_abs": 3.05},
        "institutional_holding": {
            "votes": {"shareholder": {"raw": {"change_pct": 0.0}}},
        },
    }
    tech.update(overrides)
    return tech


def test_shareholder_dispersion_display_uses_raw_fraction():
    from src.analyzers.timing_engine import TimingEngine

    gate = TimingEngine(backtest_mode=True)._defensive_chase_gates(
        _base_tech(institutional_holding={
            "votes": {"shareholder": {"raw": {"change_pct": 0.332}}},
        }),
        "main_trend",
    )
    evidence = "\n".join(gate["evidence"])
    assert "筹码分散 33.2%/20.0% 超66%" in evidence
    assert any(
        item["item"] == "筹码分散" and pytest.approx(item["gap"], abs=0.5) == 66
        for item in gate["gap_items"]
    )


def test_shareholder_dispersion_compliant_shows_check():
    from src.analyzers.timing_engine import TimingEngine

    gate = TimingEngine(backtest_mode=True)._defensive_chase_gates(
        _base_tech(
            fundamental={"profit_yoy": 8.0, "profit_abs": 3.05},
            institutional_holding={
                "votes": {"shareholder": {"raw": {"change_pct": -0.193}}},
            },
        ),
        "main_trend",
    )
    evidence = "\n".join(gate["evidence"])
    assert "筹码分散 -19.3%/20.0% ✓" in evidence
    assert "余0%" not in evidence


def test_lhb_missing_date_column_is_honest(monkeypatch):
    import pandas as pd
    import src.analyzers.institutional_scorer as scorer

    frame = pd.DataFrame([
        {"代码": "002975", "龙虎榜净买额": 100_000_000, "上榜日": "2026-09-10"},
    ])
    monkeypatch.setattr(scorer, "_get_lhb_market_data", lambda: frame)
    result = scorer._fetch_lhb_institutional("002975")
    assert result["vote"] == 1
    assert result["raw"]["as_of"] == "2026-09-10"


def test_source_crash_falls_back_and_logs(monkeypatch):
    import src.analyzers.institutional_scorer as scorer

    log_dir = Path(".ab_tmp") / "source-crash-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"test-{uuid.uuid4().hex}.log"
    monkeypatch.setattr(
        scorer, "_log_source_exception",
        lambda code, source, exc: log_path.write_text(
            f"{code}|{source}", encoding="utf-8",
        ),
    )
    result = scorer._safe_vote_source(
        "002975", "龙虎榜",
        lambda code: (_ for _ in ()).throw(NameError("name 'x' is not defined")),
    )
    assert result["vote"] == 0
    assert result["detail"] == "龙虎榜→数据异常(已记录)"
    assert log_path.read_text(encoding="utf-8") == "002975|龙虎榜"


def _make_lifecycle():
    from src.analyzers.signal_lifecycle import InMemorySignalEventStore, SignalLifecycle

    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77,
        stop_loss=85.54, target_low=112.89, target_high=118.0,
    )
    return lifecycle, event


def test_filled_status_line_does_not_suggest_pullback():
    lifecycle, event = _make_lifecycle()
    lifecycle.evaluate_events(
        "002975", current_price=102.0,
        day_low=102.0, day_high=104.0, close_price=103.0,
        today=date(2026, 9, 9),
    )
    note = lifecycle.event_status_note("002975", current_price=103.0)
    assert "已成交第1天" in note
    assert "待回踩" not in note


def test_terminal_status_line_clears_frozen_residue():
    lifecycle, event = _make_lifecycle()
    lifecycle.evaluate_events(
        "002975", current_price=100.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    lifecycle.evaluate_events(
        "002975", current_price=583.2,
        today=date(2026, 9, 10),
    )
    note = lifecycle.event_status_note("002975", current_price=583.2)
    assert "已完结(追高放弃)" in note
    assert "完结价583.20" in note
    assert "已冻结" not in note
    assert "待回踩" not in note


def test_daily_unfreeze_batch_positive_and_negative_scores():
    lifecycle, event = _make_lifecycle()
    lifecycle.evaluate_events(
        "002975", current_price=100.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    keep = lifecycle.run_daily_unfreeze(
        {"002975": -1.0}, today=date(2026, 9, 9),
    )
    assert keep == []
    assert lifecycle.store.events[event.event_id].status == "frozen"

    notices = lifecycle.run_daily_unfreeze(
        {"002975": 1.0}, today=date(2026, 9, 9),
    )
    assert lifecycle.store.events[event.event_id].status == "valid"
    assert any(item["exit_type"] == "事件解冻" for item in notices)
    assert lifecycle.store.logs[-1]["rule_entry"] == "R4.2事件解冻"
