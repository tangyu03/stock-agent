from datetime import date

import pytest

from src.analyzers.signal_lifecycle import DbSignalEventStore, SignalLifecycle
from src.db import close_thread_connection, init_db
from src.feedback.daily_review_parts import (
    build_yesterday_observation_review,
    evaluate_environment_invalidation,
    format_invalidation_clause,
)
from src.feedback.observation_tracker import (
    build_intercept_weekly_summary,
    fill_observation_checkpoints,
    get_observation_intercepts,
    record_observation_t0,
)
from src.feedback.signal_ledger import get_signal_ledger
from src.feedback.signal_quality import (
    build_signal_quality_stats,
    format_signal_quality_card,
)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    import src.db as db_module

    db_path = tmp_path / "p3-feedback.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    close_thread_connection()
    yield db_path
    close_thread_connection()


def test_observation_t0_is_idempotent_and_checkpoints_fill(sqlite_db):
    init_db()
    rows = [
        {
            "stock_code": "002975", "stock_name": "博杰股份",
            "current_price": 100.0,
            "entry_diagnostic": "评分: 2/6",
            "intercept_reason": "确认追强：缺 ADX",
        },
        {
            "stock_code": "300666", "stock_name": "深科达",
            "current_price": 100.0,
            "entry_diagnostic": "评分: -2/6",
            "intercept_reason": "确认追强：缺 ADX",
        },
    ]
    assert record_observation_t0(rows, "2026-09-08", benchmark_close=100.0) == 2
    assert record_observation_t0(rows, "2026-09-08", benchmark_close=100.0) == 0
    saved = get_observation_intercepts("2026-09-08")
    assert [row["tech_score"] for row in saved] == [2.0, -2.0]

    result = fill_observation_checkpoints(
        as_of=date(2026, 9, 25),
        price_provider=lambda code, day: 110.0,
        benchmark_provider=lambda day: 100.0,
    )
    assert result["filled"] == 6
    assert result["missing"] == 0

    summary = build_intercept_weekly_summary()
    assert "累计样本6" in summary
    assert "T+5中位超额+10.00%" in summary
    assert "技术分≥+1组+10.00%" in summary
    assert "≤-1组+10.00%" in summary


def test_signal_outcomes_complete_filled_events_and_quality_cards(sqlite_db):
    init_db()
    lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    target_event = lifecycle.register_event(
        "002975", "博杰股份", "价量突破",
        breakout_level=99.39, entry_price=102.77, stop_loss=85.54,
        target_low=112.89, target_high=120.21,
    )
    lifecycle.evaluate_events(
        "002975", current_price=103.0, day_low=102.0,
        close_price=103.0, today=date(2026, 9, 10),
    )
    lifecycle.evaluate_events(
        "002975", current_price=115.0, day_high=118.0,
        close_price=115.0, today=date(2026, 9, 11),
    )
    target_saved = DbSignalEventStore().get_prior_events("002975")[-1]
    assert target_saved.status == "sig_target"

    stop_lifecycle = SignalLifecycle(DbSignalEventStore(), valid_days=5)
    stop_event = stop_lifecycle.register_event(
        "300666", "深科达", "价量突破",
        breakout_level=90.0, entry_price=100.0, stop_loss=95.0,
        target_low=115.0, target_high=120.0,
    )
    stop_lifecycle.evaluate_events(
        "300666", current_price=101.0, day_low=100.0,
        close_price=101.0, today=date(2026, 9, 10),
    )
    stop_lifecycle.evaluate_events(
        "300666", current_price=94.0, day_low=94.0,
        close_price=94.0, today=date(2026, 9, 11),
    )
    stop_saved = DbSignalEventStore().get_prior_events("300666")[-1]
    assert stop_saved.status == "sig_stop"

    ledger = get_signal_ledger()
    stats = build_signal_quality_stats(ledger)
    assert stats[0]["target"] == 1
    assert stats[0]["stop"] == 1
    card = format_signal_quality_card(stats[0], "975")
    assert "#975 价量突破" in card
    assert "触目标50%" in card
    assert "触止损50%" in card
    assert "样本不足(2/30)" in card


def test_yesterday_review_and_invalidation_clause(sqlite_db):
    init_db()
    record_observation_t0(
        [{
            "stock_code": "002975", "stock_name": "博杰股份",
            "current_price": 100.0,
            "entry_diagnostic": "评分: 2/6",
        }],
        "2026-09-09", benchmark_close=100.0,
    )
    line = build_yesterday_observation_review(
        "2026-09-09", {"002975": 101.0}, benchmark_change_pct=0.3,
    )
    assert "博杰股份(002975)+1.0%" in line
    assert "观察票均值+1.0%" in line

    environment = {
        "market_env": {"ad_ratio": 1.3, "turnover_ratio": 1.1},
        "gem_sci_tech": {"gem": {"current": 2100.0, "ma20": 2050.0}},
    }
    assert evaluate_environment_invalidation(environment)["triggered"] is True
    text = format_invalidation_clause(environment)
    assert "环境判定作废重估告警" in text

    missing_environment = {"market_env": {}, "gem_sci_tech": {}}
    assert evaluate_environment_invalidation(missing_environment)["triggered"] is False
    assert "缺涨跌比+量比+创业板收复MA20" in format_invalidation_clause(missing_environment)
