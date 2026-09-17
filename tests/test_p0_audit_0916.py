"""2026-09-16 P0 audit acceptance cases."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_gray_zone_outputs_reduce_not_blank(monkeypatch):
    """中际旭创 9-14 收盘 873 落在结构位/止损灰区时必须有唯一指令。"""
    from src.analyzers.timing_engine import get_backtest_timing_engine

    te = get_backtest_timing_engine()
    tech = {
        "current_price": 873.00,
        "volume_ratio": 1.00,
        "ma5": 880.0,
        "ma10": 875.0,
        "ma20": 870.0,
        "kline": [
            {"close": 873.0, "open": 874.0, "high": 876.0, "low": 870.0,
             "volume": 1000}
            for _ in range(30)
        ],
        "tech_signals": {"vote": "中性", "vote_score": 0},
        "kline_pattern": [],
    }
    paired = {
        "entry_type": "价量突破",
        "paired_z": 817.99,
        "z_reference": 889.12,
    }
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode: tech)
    monkeypatch.setattr(te, "_get_paired_position", lambda code: paired)

    signals = te.check_exit_signals("300308", "中际旭创")

    gray = [signal for signal in signals if signal.exit_type.startswith("灰区")]
    assert gray
    assert gray[0].exit_type == "灰区减仓"
    assert "跌破结构位889.12" in gray[0].reason
    assert "未到止损817.99" in gray[0].reason


def test_gray_zone_shrinking_volume_outputs_clear(monkeypatch):
    from src.analyzers.timing_engine import get_backtest_timing_engine

    te = get_backtest_timing_engine()
    tech = {
        "current_price": 873.00,
        "volume_ratio": 0.70,
        "ma5": 880.0,
        "ma10": 875.0,
        "ma20": 870.0,
        "kline": [
            {"close": 873.0, "open": 874.0, "high": 876.0, "low": 870.0,
             "volume": 1000}
            for _ in range(30)
        ],
        "tech_signals": {"vote": "中性", "vote_score": 0},
        "kline_pattern": [],
    }
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode: tech)
    monkeypatch.setattr(
        te, "_get_paired_position",
        lambda code: {"entry_type": "价量突破", "paired_z": 817.99,
                      "z_reference": 889.12},
    )

    signals = te.check_exit_signals("300308", "中际旭创")

    assert any(signal.exit_type == "灰区清仓" for signal in signals)


def _chase_tech(confirm_fund: bool):
    closes = [80.0 + index * 0.5 for index in range(25)]
    flows = [100.0, 200.0, 300.0]
    if not confirm_fund:
        flows = flows[:1]
    return {
        "current_price": 100.0,
        "ma20": 92.0,
        "prior_high": 99.0,
        "recent_high": 99.0,
        "volume_ratio": 1.3,
        "high_52w": 150.0,
        "kline": [
            {"close": close, "open": close - 0.1, "high": close + 0.2,
             "low": close - 0.2, "volume": 1000}
            for close in closes
        ],
        "institutional_holding": {
            "vote_score": 1,
            "votes": {"main_force": {
                "vote": 1,
                "raw": {
                    "net_flows_5d": flows,
                    "coverage_days": len(flows),
                    "expected_days": 3,
                    "total": sum(flows),
                    "source": "测试主力3日",
                },
            }},
        },
    }


def test_deep_pullback_requires_complete_fund_confirmation():
    from src.analyzers.timing_engine import get_backtest_timing_engine, StopLossCalc

    te = get_backtest_timing_engine()
    stop = StopLossCalc(
        stock_code="300308", current_price=100.0, support_candidates=[],
        chosen_support=90.0, stop_loss_price=87.3, resistance=110.0,
    )

    blocked_tech = _chase_tech(False)
    blocked = te._check_momentum_chase(
        "300308", "中际旭创", blocked_tech, stop, "attack", "main_trend"
    )
    confirmed = te._check_momentum_chase(
        "300308", "中际旭创", _chase_tech(True), stop, "attack", "main_trend"
    )

    assert blocked is None
    assert blocked_tech["entry_blocked_reason"].startswith("高位回落")
    assert confirmed is not None and confirmed.entry_type == "确认追强"


def test_volume_patterns_distinguish_distribution_and_divergence():
    from src.analyzers.volume_pattern import build_volume_pattern, classify

    assert classify(
        12.34, -0.031, "剧烈放量", "高位", turnover=24.5
    ) == "巨量分歧型"
    assert classify(
        1.0, 0.05, "平量", "高位", rsi6=71.9, top_divergence=True
    ) == "派发嫌疑观察"

    data = {
        "stock_code": "301598", "stock_name": "臻宝",
        "current_price": 100.0, "change_pct": 12.34,
        "volume_ratio": 3.61, "turnover_rate": 24.5,
        "prior_high": 100.0, "recent_high": 100.0,
        "gain_20d": 0.45, "ma20_falling": False,
        "ma5": 95.0, "ma10": 92.0, "ma20": 90.0,
        "tech_signals": {"order_flow": {
            "available": True, "outer_volume": 900, "inner_volume": 1000,
            "imbalance_pct": -3.1,
        }},
    }
    result = build_volume_pattern(data)
    assert result["pattern"] == "巨量分歧型"
    assert "禁止追高" in result["verdict"]


def test_partial_flow_coverage_neutralized_and_displayed():
    from src.analyzers.signal_plan import build_fund_snapshot
    from src.push.templates import _entry_decision_lines, _main_flow_window

    institutional = {
        "vote_score": 1,
        "votes": {"main_force": {
            "vote": 1,
            "raw": {
                "net_flows_5d": [500_000_000.0],
                "coverage_days": 1,
                "expected_days": 3,
                "total": 500_000_000.0,
                "source": "东财主力3日",
            },
        }},
    }
    fund = build_fund_snapshot(institutional)
    assert fund.flow_coverage_complete is False
    assert fund.main_vote == 0 and fund.vote == 0
    assert "资金覆盖1/3日,部分覆盖降权" in fund.machine_tags
    assert _main_flow_window(fund.as_dict()) == "近1日净额(覆盖1/3日,部分覆盖降权)"

    lines = _entry_decision_lines({
        "trigger_reason": "技术右侧",
        "tech_signals": {"category_votes": {
            "trend": {"vote": 1, "details": []},
            "momentum": {"vote": 0, "details": []},
            "pattern": {"vote": 0, "details": []},
        }},
        "execution_plan": {"fund_snapshot": fund.as_dict()},
    })
    joined = "\n".join(lines)
    assert "④资金:主力近1日净额(覆盖1/3日,部分覆盖降权)" in joined


def test_large_fund_technical_conflict_is_forced_into_trigger_line():
    from src.analyzers.signal_plan import build_fund_snapshot
    from src.push.templates import _entry_decision_lines

    institutional = {
        "vote_score": 1,
        "votes": {"main_force": {
            "vote": 1,
            "raw": {
                "net_flows_5d": [600_000_000.0, 700_000_000.0, 655_000_000.0],
                "coverage_days": 3,
                "expected_days": 3,
                "total": 1_955_000_000.0,
                "source": "东财主力3日",
            },
        }},
    }
    fund = build_fund_snapshot(
        institutional,
        {"tech_signals": {"vote_score": -2.0}},
    )
    assert fund.fund_tech_conflict is True
    assert "技术右侧规则优先" in fund.fund_tech_conflict_note

    lines = _entry_decision_lines({
        "trigger_reason": "多头排列",
        "tech_signals": {"category_votes": {
            "trend": {"vote": 1, "details": []},
            "momentum": {"vote": 0, "details": []},
            "pattern": {"vote": 0, "details": []},
        }},
        "execution_plan": {"fund_snapshot": fund.as_dict()},
    })
    assert any(line.startswith("⑤触发:资金-技术冲突") for line in lines)


def test_fund_snapshot_propagates_dual_source_conflict():
    from src.analyzers.signal_plan import build_fund_snapshot
    from src.push.templates import _entry_decision_lines

    institutional = {
        "vote_score": 1,
        "votes": {"main_force": {
            "vote": 0,
            "detail": "同花顺3日净额方向冲突（合计 2.29 亿，批量源）"
                      "；对账源东财主力3日-2.02亿；双源方向冲突，降为中性",
            "raw": {
                "net_flows": [229_000_000.0],
                "coverage_days": 1,
                "expected_days": 3,
                "total": 229_000_000.0,
                "source": "stock_fund_flow_individual(3日排行)",
                "cross_source": "eastmoney",
                "cross_total": -202_000_000.0,
                "source_conflict": True,
            },
        }},
    }

    fund = build_fund_snapshot(institutional)
    assert fund.source_conflict is True
    assert fund.cross_source == "eastmoney"
    assert fund.cross_total == -202_000_000.0
    assert "资金源冲突" in fund.machine_tags

    lines = _entry_decision_lines({
        "trigger_reason": "技术右侧",
        "tech_signals": {"category_votes": {
            "trend": {"vote": 1, "details": []},
            "momentum": {"vote": 0, "details": []},
            "pattern": {"vote": 0, "details": []},
        }},
        "execution_plan": {"fund_snapshot": fund.as_dict()},
    })
    assert any("④资金:" in line and "资金源冲突" in line for line in lines)


def test_observation_card_displays_partial_flow_coverage():
    from src.push.templates import _render_compact_observation_signal

    data = {
        "stock_name": "芯原股份",
        "stock_code": "688521",
        "current_price": 190.10,
        "change_pct": 2.76,
        "market_mode": "defend",
        "sector_name": "半导体",
        "sector_status": "main_trend",
        "tech_signals": {},
        "institutional_holding": {
            "vote_score": 0,
            "vote_label": "资金中性",
            "bullish_count": 0,
            "bearish_count": 0,
            "votes": {
                "main_force": {
                    "vote": 1,
                    "detail": "同花顺3日净额流入（合计 2.29 亿，批量源）",
                    "raw": {
                        "net_flows": [229_000_000.0],
                        "coverage_days": 1,
                        "expected_days": 3,
                        "total": 229_000_000.0,
                        "source": "stock_fund_flow_individual(3日排行)",
                    },
                }
            },
        },
        "note": "买入: 无 | 卖出: 无",
    }

    _, content = _render_compact_observation_signal(data)
    assert "近1日净额覆盖1/3日" in content
    assert "部分覆盖降权" in content


def test_projection_recheck_downgrades_then_invalidates_low_volume():
    from src.analyzers.signal_lifecycle import (
        InMemorySignalEventStore,
        SignalLifecycle,
    )

    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "000001", "测试", "价量突破",
        breakout_level=99.0, entry_price=100.0, stop_loss=95.0,
        target_low=110.0, target_high=115.0,
        entry_snapshot={"projection_triggered": True,
                        "projection_threshold": 2.0},
    )

    downgrades = lifecycle.apply_projection_recheck(
        {"000001": 1.0}, trade_date=date(2026, 9, 15)
    )
    assert event.status == "pending_confirm"
    assert downgrades and downgrades[0]["exit_type"] == "外推量能待确认"

    notices = lifecycle.evaluate_events(
        "000001", current_price=100.0, volume_ratio=0.71,
        today=date(2026, 9, 16),
    )
    assert event.status == "invalidated"
    assert notices and notices[0]["exit_type"] == "信号作废"
    logs = lifecycle.store.get_event_logs(event.event_id)
    assert logs[-1]["rule_entry"] == "R3.11外推量能次日不足"


def test_v29_early_interface_ratio_is_converted_to_same_period():
    from src.analyzers.signal_plan import build_volume_snapshot
    from src.analyzers.volume_pattern import build_volume_pattern, render_volume_pattern

    kline = [
        {"date": f"2026-09-{day:02d}", "volume": 1000, "turnover_rate": 1.0}
        for day in range(1, 16)
    ]
    snapshot = build_volume_snapshot({
        "kline": kline,
        "today_volume": 5000.0,
        "volume_ratio": 13.16,
        "volume_ratio_source": "行情接口",
        "volume_ratio_sample_time": "20260916094200",
        "data_date": "2026-09-16",
        "kline_includes_today": True,
    })
    assert snapshot.is_early_window is True
    assert snapshot.volume_ratio == 13.16
    assert snapshot.same_period_volume_ratio is not None
    assert snapshot.same_period_volume_ratio != 13.16
    assert snapshot.volume_ratio_effective == snapshot.same_period_volume_ratio

    pattern = build_volume_pattern({
        "change_pct": -3.0,
        "turnover_rate": 8.0,
        "current_price": 100.0,
        "ma5": 101.0,
        "ma10": 102.0,
        "ma20": 103.0,
        "prior_high": 104.0,
        "recent_high": 104.0,
        "gain_20d": 0.05,
        "ma20_falling": True,
        "tech_signals": {"order_flow": {
            "available": True,
            "outer_volume": 1000,
            "inner_volume": 500,
            "imbalance_pct": 33.3,
        }},
        "execution_plan": {"volume_snapshot": snapshot.as_dict()},
    })
    assert pattern["early_window"] is True
    assert pattern["effective_volume_ratio"] != 13.16
    rendered = "\n".join(render_volume_pattern(pattern))
    assert "同期量比7.48@09-16 09:42" in rendered
    assert "接口量比13.16@09-16 09:42" in rendered


def test_v29_interface_ratio_without_sample_time_is_not_formal_evidence():
    from src.analyzers.signal_plan import build_volume_snapshot

    kline = [
        {"date": f"2026-09-{day:02d}", "volume": 1000, "turnover_rate": 1.0}
        for day in range(1, 16)
    ]
    snapshot = build_volume_snapshot({
        "kline": kline,
        "today_volume": 5000.0,
        "volume_ratio": 13.16,
        "volume_ratio_source": "行情接口",
    })
    assert snapshot.same_period_volume_ratio is None
    assert snapshot.volume_ratio_effective is None


def test_v29_ma60_excludes_intraday_volume():
    from src.analyzers.signal_plan import build_volume_snapshot

    kline = [
        {"date": f"2026-06-{day:02d}", "volume": 1000, "turnover_rate": 1.0}
        for day in range(1, 31)
    ] + [
        {"date": f"2026-08-{day:02d}", "volume": 1000, "turnover_rate": 1.0}
        for day in range(1, 32)
    ] + [
        {"date": f"2026-09-{day:02d}", "volume": 1000, "turnover_rate": 1.0}
        for day in range(1, 16)
    ]
    snapshot = build_volume_snapshot({
        "kline": kline + [{"date": "2026-09-16", "volume": 5000}],
        "today_volume": 5000.0,
        "volume_ratio": 5.0,
        "data_date": "2026-09-16",
        "kline_includes_today": True,
    })
    assert snapshot.volume_vs_ma60 == 5.0


def test_v29_down_with_positive_drive_is_divergence_not_selling():
    from src.analyzers.volume_pattern import build_volume_pattern

    result = build_volume_pattern({
        "change_pct": -4.0,
        "volume_ratio": 2.5,
        "turnover_rate": 8.0,
        "current_price": 100.0,
        "ma5": 102.0,
        "ma10": 103.0,
        "ma20": 104.0,
        "prior_high": 105.0,
        "recent_high": 105.0,
        "gain_20d": 0.05,
        "ma20_falling": True,
        "tech_signals": {"order_flow": {
            "available": True,
            "outer_volume": 1200,
            "inner_volume": 800,
            "imbalance_pct": 20.0,
        }},
    })
    assert result["pattern"] == "放量下跌分歧型"
    assert "量价资金背离" in result["conflict"]
    assert "主动性出逃" not in result["verdict"]


def test_v29_down_with_negative_drive_remains_real_selling_pressure():
    from src.analyzers.volume_pattern import classify

    assert classify(-4.0, -0.20, "明显放量", "破位") == "真实抛压型"


def test_p19_hengdongguang_0917_is_real_selling_not_grinding():
    """P1-9：蘅东光 9-17（-5.05%/主动差-14.0%）必须归入真实抛压，禁止阴跌。

    二维判定：主动差≤-8% 或 跌幅≥3% → 真实抛压；量比≤1.0 且 跌幅≤1.5%
    → 阴跌；中间地带 → 抛压待确认型混合标签。
    """
    from src.analyzers.volume_pattern import classify

    # 蘅东光 9-17 主证据：跌幅 5.05%、主动差 -14%，任意量能档都不得贴阴跌
    for vgear in ("缩量", "平量", "温和放量", "明显放量", "剧烈放量"):
        assert classify(-5.05, -0.14, vgear, "中继") == "真实抛压型"
        assert classify(-5.05, -0.14, vgear, "破位") == "真实抛压型"

    # 边界：跌幅≥3% 或 主动差≤-8%
    assert classify(-3.0, -0.01, "缩量", "中继") == "真实抛压型"
    assert classify(-2.0, -0.10, "缩量", "中继") == "真实抛压型"

    # 阴跌窄域：量比≤1.0 且 跌幅≤1.5%
    assert classify(-1.5, -0.06, "缩量", "破位") == "阴跌不止型"
    assert classify(-1.0, -0.02, "平量", "低位") == "阴跌不止型"

    # 中间地带 → 混合标签，不得贴阴跌
    assert classify(-2.0, -0.05, "缩量", "中继") == "抛压待确认型"
    assert classify(-2.5, -0.04, "平量", "中继") == "抛压待确认型"


def test_p19_deep_drop_never_pairs_with_grinding_label():
    """P1-9 验收：跌幅>3% 的票与阴跌标签不得共存（全量回放矩阵）。"""
    from src.analyzers.volume_pattern import build_volume_pattern

    for drop in (-3.1, -4.0, -5.05, -7.5):
        for drive_pct in (-1.0, -5.0, -14.0):
            for vr in (0.7, 1.0, 1.8, 3.0, 5.5):
                data = {
                    "stock_code": "430047", "stock_name": "蘅东光",
                    "current_price": 10.0, "change_pct": drop,
                    "volume_ratio": vr, "turnover_rate": 3.0,
                    "ma5": 10.5, "ma10": 10.2, "ma20": 9.8,
                    "prior_high": 12.0, "recent_high": 12.0,
                    "gain_20d": 0.05, "ma20_falling": False,
                    "tech_signals": {"order_flow": {
                        "available": True,
                        "outer_volume": 400, "inner_volume": 600,
                        "imbalance_pct": drive_pct,
                    }},
                }
                result = build_volume_pattern(data)
                assert result["pattern"] == "真实抛压型"


def test_p18_margin_drive_divergence_forces_adjudication_line():
    """P1-8：臻宝 9-17 两融+11.3% 逆势加杠杆 vs 主动差-14% → 强制输出裁决行。"""
    from src.analyzers.signal_plan import build_fund_snapshot
    from src.push.templates import _entry_decision_lines

    institutional = {
        "vote_score": 1,
        "votes": {"north_bound": {
            "vote": 1,
            "detail": "融资余额增加11.3%（2.35→2.61亿）",
            "raw": {"latest": 2.61e8, "prev": 2.35e8, "change_pct": 0.113,
                    "as_of": "20260916"},
        }},
    }
    tech = {"tech_signals": {"order_flow": {
        "available": True, "outer_volume": 400, "inner_volume": 600,
        "imbalance_pct": -14.0,
    }}}
    fund = build_fund_snapshot(institutional, tech)
    assert fund.margin_drive_conflict is True
    assert "裁决" in fund.margin_drive_conflict_note
    assert "11.3%" in fund.margin_drive_conflict_note
    assert "两融/主动差背离" in fund.machine_tags

    lines = _entry_decision_lines({
        "trigger_reason": "技术右侧",
        "tech_signals": {"category_votes": {
            "trend": {"vote": 1, "details": []},
            "momentum": {"vote": 0, "details": []},
            "pattern": {"vote": 0, "details": []},
        }},
        "execution_plan": {"fund_snapshot": fund.as_dict()},
    })
    joined = "\n".join(lines)
    assert "⑥裁决" in joined
    assert "11.3%" in joined and "-14.0%" in joined


def test_p18_observation_card_shows_margin_drive_adjudication():
    """P1-8：观察卡 ④资金 同样强制输出两融/主动差裁决行。"""
    from src.push.templates import _institutional

    content = _institutional({
        "institutional_holding": {
            "vote_score": 1,
            "vote_label": "资金看多",
            "bullish_count": 1, "bearish_count": 0,
            "votes": {"north_bound": {
                "vote": 1,
                "detail": "融资余额增加11.3%（2.35→2.61亿）",
                "raw": {"latest": 2.61e8, "prev": 2.35e8,
                        "change_pct": 0.113, "as_of": "20260916"},
            }},
        },
        "tech_signals": {"order_flow": {
            "available": True, "outer_volume": 400, "inner_volume": 600,
            "imbalance_pct": -14.0,
        }},
    })
    assert "裁决" in content
    assert "两融逆势加杠杆" in content


def test_p18_no_adjudication_when_sources_agree_or_mild():
    """P1-8 反向验收：同向或幅度不足时不得输出裁决行。"""
    from src.analyzers.signal_plan import build_fund_snapshot

    institutional = {
        "vote_score": 1,
        "votes": {"north_bound": {
            "vote": 1, "detail": "融资余额增加",
            "raw": {"latest": 2.61e8, "prev": 2.35e8, "change_pct": 0.113,
                    "as_of": "20260916"},
        }},
    }
    # 两融+11.3% 与 主动买入+14% 同向 → 无裁决
    agree = build_fund_snapshot(institutional, {"tech_signals": {"order_flow": {
        "available": True, "outer_volume": 600, "inner_volume": 400,
        "imbalance_pct": 14.0,
    }}})
    assert agree.margin_drive_conflict is False
    # 主动差幅度不足(-4% > -5%) → 无裁决
    mild = build_fund_snapshot(institutional, {"tech_signals": {"order_flow": {
        "available": True, "outer_volume": 480, "inner_volume": 520,
        "imbalance_pct": -4.0,
    }}})
    assert mild.margin_drive_conflict is False


def test_v29_early_missing_volume_does_not_blind_high_distribution_evidence():
    from src.analyzers.volume_pattern import build_volume_pattern

    result = build_volume_pattern({
        "change_pct": 1.0,
        "volume_ratio": 13.16,
        "turnover_rate": 8.0,
        "rsi6": 72.9,
        "current_price": 117.0,
        "ma5": 110.0,
        "ma10": 108.0,
        "ma20": 105.0,
        "prior_high": 120.0,
        "recent_high": 120.0,
        "gain_20d": 0.45,
        "ma20_falling": False,
        "tech_signals": {
            "order_flow": {"available": True, "outer_volume": None},
            "chan_divergence": {"type": "顶背驰"},
        },
        "execution_plan": {"volume_snapshot": {
            "volume_ratio": 13.16,
            "volume_ratio_effective": None,
            "is_early_window": True,
            "same_period_volume_ratio": None,
            "volume_ratio_caliber": "接口量比(5日分钟均量)",
            "volume_ratio_sample_time": "09:42",
            "same_period_caliber": "同期量比数据未取到",
            "turnover_rate": 8.0,
        }},
    })
    assert result["pattern"] == "派发嫌疑观察"
    assert result["star"] == 3
    assert result["evidence_sources"] == ["位置", "RSI", "背驰"]
    assert result["effective_volume_ratio"] is None


def test_v29_output_has_no_internal_codename_or_unshipped_feature():
    from src.analyzers.volume_pattern import _PATTERN_DEFS

    assert "分时二期接入" not in " | ".join(
        item["confirm_target"] for item in _PATTERN_DEFS.values()
    )


def test_projection_recheck_restores_prior_state_when_volume_recovers():
    from src.analyzers.signal_lifecycle import (
        InMemorySignalEventStore,
        SignalLifecycle,
    )

    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "000002", "测试", "价量突破",
        breakout_level=99.0, entry_price=100.0, stop_loss=95.0,
        target_low=110.0, target_high=115.0,
        entry_snapshot={"projection_triggered": True,
                        "projection_threshold": 2.0},
    )
    lifecycle.mark_filled(event.event_id)
    lifecycle.apply_projection_recheck(
        {"000002": 1.0}, trade_date=date(2026, 9, 15)
    )
    assert event.status == "pending_confirm"
    lifecycle.record_daily_snapshot = lambda *args, **kwargs: True

    notices = lifecycle.evaluate_events(
        "000002", current_price=100.0, volume_ratio=1.2,
        today=date(2026, 9, 16),
    )
    assert event.status == "filled"
    assert notices and notices[0]["exit_type"] == "外推量能确认"
    logs = lifecycle.store.get_event_logs(event.event_id)
    assert logs[-1]["rule_entry"] == "R3.12外推量能确认恢复"


def test_p03_early_window_downgrades_volume_driven_pattern():
    """P0-3：早盘窗口量比/主动差只展示不计票，量能驱动分型降级为观察态。

    9-16 09:42 快照（大普微/盛合晶微/兆易创新类：下跌+内盘占优）曾输出
    "真实抛压型"与"防加速破位"判读句，当日全部反向。验收要求早盘不得
    输出实质判读，分型栏应全为观察态。
    """
    from src.analyzers.volume_pattern import build_volume_pattern

    result = build_volume_pattern({
        "change_pct": -4.0,
        "turnover_rate": 8.0,
        "current_price": 100.0,
        "ma5": 101.0,
        "ma10": 102.0,
        "ma20": 103.0,
        "prior_high": 104.0,
        "recent_high": 104.0,
        "gain_20d": 0.05,
        "ma20_falling": True,
        "tech_signals": {"order_flow": {
            "available": True,
            "outer_volume": 500,
            "inner_volume": 1000,
            "imbalance_pct": -33.3,
        }},
        "execution_plan": {"volume_snapshot": {
            "volume_ratio": 13.16,
            "volume_ratio_effective": 7.48,
            "is_early_window": True,
            "same_period_volume_ratio": 7.48,
            "volume_ratio_caliber": "接口量比(5日分钟均量)",
            "volume_ratio_sample_time": "09:42",
            "same_period_caliber": "同期累计量比",
            "same_period_sample_time": "09:42",
            "turnover_rate": 8.0,
        }},
    })
    assert result["early_window"] is True
    assert result["pattern"] == "早盘观察"
    assert result["star"] == 1
    assert "待10:30复核" in result["verdict"]
    assert "真实抛压" not in result["pattern"]


def test_p03_early_window_keeps_non_volume_observation_pattern():
    """P0-3：早盘缺同期量比时，位置/RSI/背驰驱动的派发嫌疑观察不得连坐。"""
    from src.analyzers.volume_pattern import build_volume_pattern

    result = build_volume_pattern({
        "change_pct": 1.0,
        "volume_ratio": 13.16,
        "turnover_rate": 8.0,
        "rsi6": 72.9,
        "current_price": 117.0,
        "ma5": 110.0,
        "ma10": 108.0,
        "ma20": 105.0,
        "prior_high": 120.0,
        "recent_high": 120.0,
        "gain_20d": 0.45,
        "ma20_falling": False,
        "tech_signals": {
            "order_flow": {"available": True, "outer_volume": None},
            "chan_divergence": {"type": "顶背驰"},
        },
        "execution_plan": {"volume_snapshot": {
            "volume_ratio": 13.16,
            "volume_ratio_effective": None,
            "is_early_window": True,
            "same_period_volume_ratio": None,
            "volume_ratio_caliber": "接口量比(5日分钟均量)",
            "volume_ratio_sample_time": "09:42",
            "same_period_caliber": "同期量比数据未取到",
            "turnover_rate": 8.0,
        }},
    })
    assert result["pattern"] == "派发嫌疑观察"
    assert result["star"] == 3


def test_p03_afternoon_keeps_real_selling_pressure():
    """P0-3：10:30 后恢复正常计票，真实抛压判读允许输出。"""
    from src.analyzers.volume_pattern import build_volume_pattern

    result = build_volume_pattern({
        "change_pct": -4.0,
        "volume_ratio": 7.48,
        "turnover_rate": 8.0,
        "current_price": 100.0,
        "ma5": 101.0,
        "ma10": 102.0,
        "ma20": 103.0,
        "prior_high": 104.0,
        "recent_high": 104.0,
        "gain_20d": 0.05,
        "ma20_falling": True,
        "tech_signals": {"order_flow": {
            "available": True,
            "outer_volume": 500,
            "inner_volume": 1000,
            "imbalance_pct": -33.3,
        }},
        "execution_plan": {"volume_snapshot": {
            "volume_ratio": 7.48,
            "volume_ratio_effective": 7.48,
            "is_early_window": False,
            "volume_ratio_caliber": "接口量比",
            "volume_ratio_sample_time": "14:07",
            "turnover_rate": 8.0,
        }},
    })
    assert result["early_window"] is False
    assert result["pattern"] == "真实抛压型"
    assert result["star"] == 3


def test_p17_full_coverage_still_shows_expected_window():
    """P1-7：覆盖3/3才允许显示3日窗口字样。"""
    from src.analyzers.signal_plan import build_fund_snapshot
    from src.push.templates import _main_flow_window

    institutional = {
        "vote_score": 1,
        "votes": {"main_force": {
            "vote": 1,
            "raw": {
                "net_flows_5d": [100_000_000.0, 200_000_000.0, 300_000_000.0],
                "coverage_days": 3,
                "expected_days": 3,
                "total": 600_000_000.0,
                "source": "东财主力3日",
            },
        }},
    }
    fund = build_fund_snapshot(institutional)
    assert fund.flow_coverage_complete is True
    assert _main_flow_window(fund.as_dict()) == "3日窗口(覆盖3/3日)"


def test_p17_partial_coverage_never_labels_expected_days():
    """P1-7：覆盖1/3日不得出现"3日净额(覆盖1/3日)"式组合。"""
    from src.push.templates import _institutional

    content = _institutional({"institutional_holding": {
        "vote_score": 0,
        "vote_label": "资金中性",
        "votes": {"main_force": {
            "vote": 1,
            "detail": "同花顺3日净额流入（合计 2.29 亿，批量源）",
            "raw": {
                "net_flows": [229_000_000.0],
                "coverage_days": 1,
                "expected_days": 3,
                "total": 229_000_000.0,
                "source": "stock_fund_flow_individual(3日排行)",
            },
        }},
    }})
    assert "近1日净额覆盖1/3日" in content
    assert "3日窗口覆盖1/3日" not in content


def test_p05_suspect_enters_on_high_overbought_divergence():
    """P0-5：无→嫌疑（高位+RSI6>70+顶背驰）。"""
    from src.feedback.distribution_watch import (
        STATE_NONE,
        STATE_SUSPECT,
        evaluate_distribution_state,
    )

    r = evaluate_distribution_state(
        STATE_NONE, rsi6=72.9, top_divergence=True,
    )
    assert r["state"] == STATE_SUSPECT
    assert r["changed"] is True
    assert "顶背驰" in r["reason"]


def test_p05_suspect_confirmed_on_price_down():
    """P0-5：嫌疑→确认（价格下行，优先于 RSI 滞后解除）。"""
    from src.feedback.distribution_watch import (
        STATE_CONFIRMED,
        STATE_SUSPECT,
        evaluate_distribution_state,
    )

    r = evaluate_distribution_state(
        STATE_SUSPECT, rsi6=55.7, top_divergence=False,
        price_change_pct=-5.05,
    )
    assert r["state"] == STATE_CONFIRMED
    assert r["changed"] is True
    assert "价格下行" in r["reason"]


def test_p05_suspect_released_when_rsi6_recovers_without_breakdown():
    """P0-5：嫌疑→解除（RSI6回落70下方且无价格下行）。"""
    from src.feedback.distribution_watch import (
        STATE_RELEASED,
        STATE_SUSPECT,
        evaluate_distribution_state,
    )

    r = evaluate_distribution_state(
        STATE_SUSPECT, rsi6=55.7, top_divergence=False,
        price_change_pct=-0.5,
    )
    assert r["state"] == STATE_RELEASED
    assert r["changed"] is True
    assert "RSI6回落70下方" in r["reason"]


def test_p05_suspect_keeps_when_no_new_evidence():
    """P0-5：无新证据时嫌疑延续，不得静默消失。"""
    from src.feedback.distribution_watch import (
        STATE_SUSPECT,
        evaluate_distribution_state,
    )

    r = evaluate_distribution_state(
        STATE_SUSPECT, rsi6=71.0, top_divergence=True,
        price_change_pct=0.0,
    )
    assert r["state"] == STATE_SUSPECT
    assert r["changed"] is False
    assert "延续" in r["rule"]


def test_p05_hengdongguang_0916_0917_replay():
    """P0-5：蘅东光 9-16 嫌疑 → 9-17 必须输出状态转换（不得静默）。
    9-16 RSI6=72.9+顶背驰挂嫌疑；9-17 RSI6=55.7 且价格 -5.05%
    归入真实抛压 → 状态机输出确认（派发兑现）。"""
    from src.feedback.distribution_watch import (
        STATE_CONFIRMED,
        STATE_SUSPECT,
        evaluate_distribution_state,
    )

    day1 = evaluate_distribution_state(
        "无", rsi6=72.9, top_divergence=True,
    )
    assert day1["state"] == STATE_SUSPECT
    day2 = evaluate_distribution_state(
        day1["state"], rsi6=55.7, top_divergence=True,
        price_change_pct=-5.05,
    )
    assert day2["state"] == STATE_CONFIRMED
    assert day2["changed"] is True
    assert day2["reason"] != ""


def test_p06_block_trade_stats_parse_and_trigger():
    """P0-6：大宗明细解析（mock DataFrame）+ 折价超阈值触发派发确认候选。"""
    import pandas as pd

    from src.data_layer.block_trade import (
        block_trade_distribution_trigger,
        fetch_block_trade_stats,
    )

    rows = [
        {"交易日期": "2026-09-16", "证券代码": "430047",
         "证券简称": "蘅东光", "成交价": 9.7, "折溢率": -3.2,
         "成交量": 40000, "成交额": 388000.0,
         "卖方营业部": "机构专用"},
        {"交易日期": "2026-09-15", "证券代码": "430047",
         "证券简称": "蘅东光", "成交价": 9.8, "折溢率": -4.0,
         "成交量": 30000, "成交额": 294000.0,
         "卖方营业部": "机构专用"},
    ]
    df = pd.DataFrame(rows)

    def fake_fetcher(start_date, end_date):
        return df

    stats = fetch_block_trade_stats("430047", days=20, fetcher=fake_fetcher)
    assert stats is not None
    assert stats["trades"] == 2
    assert stats["discount_trades"] == 2
    assert stats["total_amount"] > 0
    assert stats["as_of"] == "2026-09-16"

    trigger = block_trade_distribution_trigger(stats, float_cap=1e9)
    assert trigger["triggered"] is False  # 2笔未达5笔，且金额<1%流通市值

    many = dict(stats)
    many["discount_trades"] = 5
    trigger5 = block_trade_distribution_trigger(many, float_cap=1e9)
    assert trigger5["triggered"] is True
    assert "5笔" in trigger5["reason"]

    big = dict(stats)
    big["discount_total"] = 30_000_000.0
    trigger_big = block_trade_distribution_trigger(big, float_cap=1e9)
    assert trigger_big["triggered"] is True
    assert "1%" in trigger_big["reason"]


def test_p06_block_trade_line_rendered_with_warning():
    """P0-6：资金卡输出大宗交易栏及派发预警（蘅东光 9-16 回放）。"""
    from src.push.templates import _institutional

    content = _institutional({"institutional_holding": {
        "vote_score": 0,
        "vote_label": "资金中性",
        "votes": {},
        "block_trade": {
            "trades": 10,
            "discount_trades": 8,
            "total_amount": 209_000_000.0,
            "discount_total": 200_000_000.0,
            "as_of": "2026-09-16",
            "days": 20,
            "seller_consistency": {"机构专用": 8, "某营业部": 2},
            "trigger": {
                "triggered": True,
                "reason": "近20日折价大宗8笔≥5笔；折价累计2.00亿>流通市值1%",
            },
        },
    }})
    assert "大宗:近20日大宗10笔" in content
    assert "折价8笔" in content
    assert "营业部持续" in content
    assert "派发确认候选" in content


def test_p05_distribution_state_persists_and_renders_transition(tmp_path, monkeypatch):
    """P0-5：蘅东光 9-16 嫌疑 → 9-17 确认，复盘渲染必须输出转换，不得静默。"""
    import src.db as db_module

    db_path = tmp_path / "p05-watch.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.close_thread_connection()
    db_module.init_db()

    from src.feedback.distribution_watch import (
        STATE_CONFIRMED,
        STATE_SUSPECT,
        record_distribution_state,
        render_distribution_summary,
    )

    # 9-16：高位+RSI6=72.9+顶背驰 → 进入嫌疑
    r1 = record_distribution_state(
        "430047", "蘅东光",
        as_of="2026-09-16", rsi6=72.9, top_divergence=True,
    )
    assert r1["state"] == STATE_SUSPECT
    assert r1["changed"] is True

    # 9-17：RSI6 回落至 55.7 但价格 -5.05%（真实抛压）→ 派发确认
    r2 = record_distribution_state(
        "430047", "蘅东光",
        as_of="2026-09-17", rsi6=55.7, top_divergence=True,
        price_change_pct=-5.05,
    )
    assert r2["state"] == STATE_CONFIRMED
    assert r2["changed"] is True

    summary = render_distribution_summary(as_of="2026-09-17")
    assert "派发状态机" in summary
    assert "嫌疑" in summary
    assert "确认" in summary
    assert "价格下行" in summary


def test_p05_yesterday_suspect_appears_today_state(tmp_path, monkeypatch):
    """P0-5 审计：昨日嫌疑票今日必须出现在状态机任一终态或延续态。"""
    import src.db as db_module

    db_path = tmp_path / "p05-watch2.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.close_thread_connection()
    db_module.init_db()

    from src.feedback.distribution_watch import (
        STATE_SUSPECT,
        list_active_suspects,
        record_distribution_state,
        render_distribution_summary,
    )

    record_distribution_state(
        "688361", "中科飞测",
        as_of="2026-09-16", rsi6=75.8, top_divergence=True,
    )
    record_distribution_state(
        "688361", "中科飞测",
        as_of="2026-09-17", rsi6=74.0, top_divergence=True,
        price_change_pct=1.2,
    )

    y_suspects = list_active_suspects(as_of="2026-09-16")
    assert any(s["stock_code"] == "688361" for s in y_suspects)
    # 今日无新证据 → 延续嫌疑（不得静默消失）
    summary = render_distribution_summary(as_of="2026-09-17")
    assert "昨日嫌疑今日" in summary
    assert "延续嫌疑" in summary or STATE_SUSPECT in summary
