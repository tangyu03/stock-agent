from pathlib import Path

from src.analyzers.signal_plan import (
    build_execution_plan,
    build_fund_snapshot,
    build_volume_snapshot,
)
from src.decision.live_scheduler import schedule_live_signals
from src.push.templates import _execution_plan
from src.push.templates import render_entry_signal
from src.push.templates import render_exit_signal
from src.orchestrator.unified_engine import _volume_breakout_blocker


def test_volume_snapshot_uses_prior_day_quantiles():
    kline = []
    for index in range(70):
        kline.append({
            "volume": 1000 + index,
            "turnover_rate": 1.0 + index * 0.1,
        })
    kline[-1]["turnover_rate"] = 100.0
    tech_data = {
        "kline": kline,
        "today_volume": 5000,
        "volume_ratio": 3.0,
        "turnover_rate": 100.0,
    }

    snapshot = build_volume_snapshot(tech_data, min_samples=60)

    assert snapshot.data_ok is True
    assert snapshot.turnover_p90 < 100.0
    assert snapshot.turnover_hot is True
    assert snapshot.volume_hot is True


def test_medium_conflict_is_labeled_without_confidence_penalty():
    tech_data = {
        "current_price": 101.61,
        "ma5": 99.0,
        "ma10": 98.0,
        "ma20": 97.0,
        "tech_signals": {
            "vote_score": 0,
            "category_votes": {},
            "chan_divergence": {"type": "顶背驰", "confidence": "中"},
        },
    }

    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=98.0,
        stop_loss=93.0,
        target_range=[108.0],
        tech_data=tech_data,
    )

    assert "矛盾0:MACD顶背驰(中)" in plan.confidence_details
    assert plan.confidence_score == 0


def test_adx_is_display_only_in_confidence_details():
    tech_data = {
        "tech_signals": {"vote_score": 0},
        "adx": 28.0,
    }

    trend_plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.0],
        tech_data=tech_data,
    )
    reversal_plan = build_execution_plan(
        entry_type="恐慌抄底",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.0],
        tech_data=tech_data,
    )

    display_text = "ADX28.0(单边力度强,方向需看MACD,不评分)"
    assert display_text in trend_plan.confidence_details
    assert display_text in reversal_plan.confidence_details
    assert trend_plan.confidence_score == reversal_plan.confidence_score


def test_fund_snapshot_caps_disagreement_to_neutral_vote():
    institutional = {
        "vote_score": 2,
        "votes": {
            "main_force": {
                "vote": 1,
                "raw": {
                    "net_flows_5d": [100.0, 200.0, 300.0],
                    "super_large_flows_5d": [-100.0],
                    "large_flows_5d": [150.0],
                },
            },
            "shareholder": {"raw": {"change_pct": 0.274}},
        },
    }

    snapshot = build_fund_snapshot(institutional)

    assert snapshot.main_strong is True
    assert snapshot.disagreement is True
    assert snapshot.order_confirmation == "大资金分歧"
    assert snapshot.vote == 0
    assert snapshot.machine_tags == ["大资金分歧"]


def test_fund_snapshot_marks_institutional_shareholder_divergence():
    institutional = {
        "vote_score": 1,
        "top10_institutional_ratio": {"change_points": -3.0},
        "votes": {
            "main_force": {
                "vote": 1,
                "raw": {"net_flows_5d": [-100.0, 200.0, 300.0]},
            },
            "shareholder": {"raw": {"change_pct": 0.274}},
        },
    }

    snapshot = build_fund_snapshot(institutional)

    assert snapshot.institutional_shareholder_divergence is True
    assert "机构散户分歧" in snapshot.machine_tags


def test_fund_snapshot_marks_suspected_distribution_and_adjusts_confidence():
    kline = [{"close": 100.0 + index * 0.4} for index in range(6)]
    institutional = {
        "vote_score": 2,
        "votes": {
            "main_force": {
                "vote": 1,
                "raw": {
                    "net_flows_5d": [100.0, 200.0, 300.0, 400.0, 500.0],
                    "total": 1_500.0,
                },
            },
            "shareholder": {"raw": {"change_pct": 0.274}},
        },
    }

    snapshot = build_fund_snapshot(institutional, {"kline": kline})

    assert snapshot.suspected_distribution is True
    assert snapshot.institutional_adjustment == -1
    assert "疑似派发" in snapshot.machine_tags


def _valid_tech_data():
    kline = [
        {"volume": 1000, "turnover_rate": 5.0}
        for _ in range(61)
    ]
    kline[-1]["turnover_rate"] = 100.0
    return {
        "kline": kline,
        "today_volume": 4000,
        "volume_ratio": 4.0,
        "turnover_rate": 100.0,
        "ma5": 10.3,
        "ma10": 10.0,
        "ma20": 9.8,
        "rsi": 55,
        "adx": 30,
        "market_score": 6.0,
        "kline_pattern": [],
        "tech_signals": {
            "category_votes": {
                "trend": {"vote": 1},
                "momentum": {"vote": 1},
                "pattern": {"vote": 1},
                "volume": {"vote": 1},
            }
        },
        "institutional_holding": {
            "vote_score": 2,
            "votes": {
                "main_force": {
                    "vote": 1,
                    "raw": {
                        "net_flows_5d": [100.0, 200.0, 300.0],
                        "super_large_flows_5d": [100.0],
                        "large_flows_5d": [50.0],
                    },
                },
                "shareholder": {"raw": {"change_pct": 0.30}},
            },
        },
    }


def test_execution_plan_scores_risk_and_rrr_from_benchmark():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.5, 12.5],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
    )

    assert plan.confidence == "高"
    assert plan.confidence_score == 5
    assert plan.applicable_score == 6
    assert plan.risk_pct == 0.05
    assert plan.rrr_low == 3.0
    assert plan.rrr_high == 5.0
    assert plan.risk_multipliers["turnover_hot"] == 0.5
    assert plan.risk_multipliers["shareholder_increase"] == 0.8
    assert plan.combined_risk_multiplier == 0.4
    assert plan.execute is True


def test_rrr_below_two_blocks_high_confidence():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[10.9],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
    )

    assert plan.rrr_low < 2.0
    assert plan.confidence == "中"
    assert plan.execute is True


def test_execution_plan_builds_current_state_tiers():
    tech_data = {
        "current_price": 177.12,
        "ma5": 178.99,
        "ma10": 173.80,
    }

    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=173.80,
        stop_loss=168.58,
        target_range=[191.38],
        tech_data=tech_data,
    )

    tiers = {tier["name"]: tier for tier in plan.execution_tiers}
    assert tiers["MA10档"]["state"] == "上方"
    assert tiers["MA10档"]["trigger"] == "回踩不破173.80"
    assert tiers["MA5档"]["state"] == "已下破"
    assert tiers["MA5档"]["trigger"] == "反弹收复178.99"
    assert tiers["止损"]["state"] == "上方"
    assert tiers["止损"]["trigger"] == "回踩不破168.58"


def test_scheduler_schedules_low_confidence_for_display_only():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.5],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
    )
    low_plan = dict(plan.as_dict())
    low_plan.update({"execute": False, "confidence": "低", "combined_risk_multiplier": 1.0})
    entries = [
        {
            "stock_code": "LOW",
            "stock_name": "低置信",
            "entry_type": "价量突破",
            "trigger_price": 10.0,
            "confidence": "低",
            "execution_plan": low_plan,
        },
        {
            "stock_code": "PLAN",
            "stock_name": "执行",
            "entry_type": "价量突破",
            "trigger_price": 10.0,
            "confidence": "高",
            "benchmark_price": 10.0,
            "rrr_low": 3.0,
            "execution_plan": plan.as_dict(),
        },
    ]

    scheduled = schedule_live_signals(entries, [])

    assert [item.stock_code for item in scheduled["buy"]] == ["PLAN", "LOW"]
    assert scheduled["buy"][0].shares == 10_000
    assert scheduled["buy"][0].risk_multiplier == 0.4
    assert scheduled["buy"][1].shares == 25_000
    assert scheduled["buy"][1].confidence == "低"
    assert not scheduled["skipped"]["buy_low_confidence"]


def test_scheduler_uses_main_tier_and_defend_caps_industry_boost():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.5],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
        sector_name="半导体",
    )
    plan_dict = plan.as_dict()
    plan_dict.update({
        "combined_risk_multiplier": 1.0,
        "industry_multiplier": 1.2,
        "industry_tags": ["半导体:disclosed"],
    })

    scheduled = schedule_live_signals(
        [{
            "stock_code": "PLAN",
            "stock_name": "执行",
            "entry_type": "价量突破",
            "trigger_price": 10.4,
            "position_level": "heavy",
            "confidence": "高",
            "execution_plan": plan_dict,
        }],
        [],
        market_mode="defend",
    )

    assert scheduled["buy"][0].shares == 25_000
    assert scheduled["buy"][0].industry_multiplier == 1.0
    assert "主档10.00" in scheduled["buy"][0].schedule_note


def test_industry_tuning_reads_declarative_events():
    config_path = Path(__file__).with_name("_industry_tuning_test.yaml")
    config_path.write_text(
        """
industry_events:
  - sector: 半导体
    status: disclosed
    expires: '2999-12-31'
    note: 产线批量交付
  - sector: 半导体
    status: unverified
    note: 订单待确认
""",
        encoding="utf-8",
    )

    try:
        from src.analyzers.signal_plan import _industry_tuning

        multiplier, tags = _industry_tuning("半导体", config_path)
        assert multiplier == 0.8
        assert len(tags) == 2
    finally:
        config_path.unlink(missing_ok=True)


def test_conflict_uses_pattern_vote_not_stale_pattern_count():
    from src.analyzers.signal_plan import _signal_conflicts

    tech_data = {
        "kline_pattern": [
            {"signal": "看跌", "strength": 100},
        ],
        "tech_signals": {
            "category_votes": {
                "pattern": {"vote": 0},
            }
        },
    }

    assert _signal_conflicts(tech_data) == []


def test_volume_breakout_blocker_reads_single_volume_snapshot():
    kline = [{"volume": 1_000.0} for _ in range(60)]
    tech_data = {
        "kline": kline,
        "current_price": 10.0,
        "ma25": 9.0,
        "prev_close": 9.8,
        "today_volume": 1_200.0,
        "volume_ma60": 1_000.0,
    }

    assert _volume_breakout_blocker(tech_data, "defend") == ""


def test_template_renders_execution_plan():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=173.80,
        stop_loss=168.58,
        target_range=[191.38],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
    )

    rendered = _execution_plan({"execution_plan": plan.as_dict()})

    assert "基准:173.80" in rendered
    assert "RRR1:3.37" in rendered
    assert "换手:100.00%" in rendered
    assert "大资金分歧" not in rendered


def test_entry_render_uses_compact_execution_format():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=173.80,
        stop_loss=168.58,
        target_range=[191.38],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
    )
    plan_dict = plan.as_dict()
    plan_dict.update({
        "base_shares": 1_400,
        "suggested_shares": 800,
        "execution_tiers": [
            {"name": "MA10档", "price": 173.80, "state": "上方", "trigger": "回踩不破173.80"},
            {"name": "MA5档", "price": 178.99, "state": "已下破", "trigger": "反弹收复178.99"},
            {"name": "止损", "price": 168.58, "state": "上方", "trigger": "回踩不破168.58"},
        ],
    })
    data = {
        "stock_name": "汇成真空",
        "stock_code": "301392",
        "entry_type": "价量突破",
        "current_price": 177.12,
        "change_pct": 2.38,
        "market_mode": "defend",
        "sector_name": "通用设备",
        "sector_status": "main_trend",
        "trigger_reason": "多头排列+站上MA25+量能突破",
        "rsi": 54.8,
        "tech_signals": {
            "category_votes": {
                "trend": {"vote": 1, "details": ["MACD金叉延续"]},
                "momentum": {"vote": 0, "details": []},
                "pattern": {"vote": 1, "details": ["K线吞没看多"]},
                "volume": {"vote": 0, "details": []},
            }
        },
        "shares": 800,
        "execution_plan": plan_dict,
    }

    _, rendered = render_entry_signal(data)

    assert "现价177.12 +2.38%" in rendered
    assert "①方向:MACD金叉延续" in rendered
    assert "⑤触发:多头排列" in rendered
    assert "分档" in rendered
    assert "回踩不破173.80" in rendered
    assert "仓位链路" in rendered
    assert "技术面" not in rendered
    assert "信号逻辑" not in rendered
    assert "风控参数" not in rendered


def test_observation_render_uses_compact_reasoning_format():
    data = {
        "stock_name": "测试股",
        "stock_code": "600001",
        "current_price": 369.02,
        "change_pct": -0.22,
        "market_mode": "defend",
        "market_score": 5.0,
        "sector_name": "通信设备",
        "sector_status": "rotational",
        "rsi": 30.3,
        "volume_ratio": 0.86,
        "turnover_rate": 0.80,
        "tech_signals": {
            "category_votes": {
                "trend": {"vote": -1, "details": ["MACD死叉延续", "ADX41趋势强劲"]},
                "momentum": {"vote": 0, "details": ["RSI弱势(30)"]},
                "pattern": {"vote": 0, "details": ["K线偏涨但不足"]},
                "volume": {"vote": -1, "details": ["放量滞涨"]},
            }
        },
        "institutional_holding": {
            "vote_score": -3,
            "vote_label": "机构看空",
            "bullish_count": 0,
            "bearish_count": 3,
            "votes": {
                "north_bound": {"vote": -1, "detail": "融资余额减少"},
                "lhb": {"vote": 0, "detail": "近30日无记录"},
                "main_force": {"vote": -1, "detail": "3日净流出"},
                "shareholder": {"vote": -1, "detail": "股东户数增加"},
            },
        },
        "note": "买入: 技术投票偏空，未触发买入 | 卖出: 止损未触发",
    }

    _, rendered = render_exit_signal(data)

    assert "市场:防守(5.0)" in rendered
    assert "①方向:MACD死叉延续" in rendered
    assert "②时机:RSI30.3(不投票)" in rendered
    assert "③量能:量比0.86" in rendered
    assert "④资金:🟢机构看空" in rendered or "机构看空(-3票" in rendered
    assert "⑤拦截:技术投票偏空" in rendered
    assert "⑥风控:止损未触发" in rendered
    assert "技术面" not in rendered


def test_template_renders_machine_tags():
    rendered = _execution_plan(
        {
            "execution_plan": {
                "fund_snapshot": {
                    "machine_tags": ["大资金分歧", "疑似派发"],
                }
            }
        }
    )

    assert "机器标签:大资金分歧/疑似派发" in rendered
