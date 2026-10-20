from pathlib import Path

import pytest

from src.analyzers.signal_plan import (
    build_execution_plan,
    build_fund_snapshot,
    build_volume_snapshot,
)
from src.decision.live_scheduler import schedule_live_signals
from src.push.templates import _execution_plan
from src.push.templates import render_entry_signal
from src.orchestrator.unified_engine import _volume_breakout_blocker
from src.push.templates import render_exit_signal


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


def test_dirty_volume_snapshot_is_blocked_by_breakout():
    kline = [{"volume": 100, "turnover_rate": 1.0} for _ in range(61)]
    tech_data = {
        "kline": kline,
        "today_volume": 2000,
        "volume_ratio": 0.8,
        "current_price": 101.0,
        "ma25": 90.0,
    }

    snapshot = build_volume_snapshot(tech_data)

    assert snapshot.dirty is True
    assert snapshot.volume_ratio < 1.0
    assert snapshot.volume_vs_ma60 > 10.0
    assert snapshot.label == "量能脏数据"
    assert "量能口径冲突" in snapshot.dirty_reason
    assert snapshot.dirty_reason == _volume_breakout_blocker(tech_data, "defend")


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

    assert "矛盾0:价格顶背驰(中)" in plan.confidence_details
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
            hypothesis_x="测试买入理由:放量突破MA25",
    )

    assert plan.confidence == "高"
    # 【P0-2】RRR3.0≥2.5 质量票 +1（隐含胜率 28.6%，出处=风险预算非惯例）
    assert plan.confidence_score == 6
    assert any("RRR3.00" in d and "+1" in d for d in plan.confidence_details)
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
            hypothesis_x="测试买入理由:放量突破MA25",
    )

    assert plan.rrr_low < 2.0
    assert plan.confidence == "中"
    assert plan.execute is True


def test_execution_plan_builds_current_state_tiers():
    """策略感知分档（层间接口修复）：追强类主档=触发位、试探=浅回踩档。

    旧实现一律 MA10/MA5 模板——确认追强 Y 挂在 MA10 上（蘅东光 9/8
    距现价 10.3%），策略讲追强、档位给低吸。追强类现在主档贴近触发位。
    """
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
    assert tiers["追强档"]["state"] == "上方"
    assert tiers["追强档"]["trigger"] == "回踩不破173.80"
    # 浅回踩档 = 触发位下方 2%（173.80×0.98=170.32），MA5 在触发位上方故不用
    assert tiers["浅回踩档"]["price"] == pytest.approx(170.324, abs=0.01)
    assert tiers["浅回踩档"]["state"] == "上方"
    assert tiers["止损"]["state"] == "上方"
    assert tiers["止损"]["trigger"] == "回踩不破168.58"


def test_dip_strategy_keeps_ma_tiers():
    """低吸类（套利低吸）保留 MA10 主档 + MA5 试探档（均线承接语义）。"""
    tech_data = {
        "current_price": 177.12,
        "ma5": 178.99,
        "ma10": 173.80,
    }

    plan = build_execution_plan(
        entry_type="套利低吸",
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


def test_scheduler_schedules_low_confidence_for_display_only():
    """低置信（假说完整）仍可调度；execute=False（假说被拒）不进调度转 skipped。

    【一】新语义：execution_plan.execute=False 表示假说出厂被拒
    （缺 X/Y/Z/W、止损倒挂、缓冲不足），调度器兜底拦截。
    """
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.5],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
        hypothesis_x="测试买入理由:放量突破MA25",
    )
    low_plan = dict(plan.as_dict())
    low_plan.update({"execute": False, "confidence": "低", "combined_risk_multiplier": 1.0})
    # 低置信但假说完整：execute 保持 True
    low_conf_complete = dict(plan.as_dict())
    low_conf_complete.update({"confidence": "低", "combined_risk_multiplier": 1.0})
    entries = [
        {
            "stock_code": "LOW",
            "stock_name": "低置信",
            "entry_type": "价量突破",
            "trigger_price": 10.0,
            "confidence": "低",
            "execution_plan": low_conf_complete,
        },
        {
            "stock_code": "REJ",
            "stock_name": "被拒",
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

    # 假说被拒（execute=False）→ 不进调度，进 skipped 留痕
    assert [item.stock_code for item in scheduled["buy"]] == ["PLAN", "LOW"]
    assert scheduled["buy"][0].shares == 10_000
    assert scheduled["buy"][0].risk_multiplier == 0.4
    assert scheduled["buy"][1].shares == 25_000
    assert scheduled["buy"][1].confidence == "低"
    assert not scheduled["skipped"]["buy_low_confidence"]
    assert [s["stock_code"] for s in scheduled["skipped"]["buy_hypothesis_rejected"]] == ["REJ"]
    assert scheduled["stats"]["buy_hypothesis_rejected"] == 1


def test_score_gate_reason_discloses_score_components():
    sig = {
        "stock_code": "GATE01", "stock_name": "天孚通信",
        "entry_type": "恐慌抄底", "trigger_price": 194.61,
        "execution_plan": {
            "confidence_score": 1, "applicable_score": 6,
            "confidence_details": ["RRR0.20<1.5降档"],
        },
    }

    scheduled = schedule_live_signals([sig], [])

    assert scheduled["stats"]["buy_score_gate"] == 1
    reason = scheduled["warnings"]["buy_score_gate"][0]["reason"]
    assert scheduled["buy"][0].stock_code == "GATE01"
    assert scheduled["skipped"]["buy_score_gate"] == []
    assert "综合评分1/6" in reason
    assert "RRR0.20<1.5降档" in reason


def test_scheduler_uses_main_tier_and_defend_caps_industry_boost():
    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=10.0,
        stop_loss=9.5,
        target_range=[11.5],
        tech_data=_valid_tech_data(),
        sector_status="main_trend",
            hypothesis_x="测试买入理由:放量突破MA25",
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
            hypothesis_x="测试买入理由:放量突破MA25",
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
            hypothesis_x="测试买入理由:放量突破MA25",
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


def test_fixed_card_renders_nearest_key_levels():
    from src.push.templates import _key_levels

    data = {
        "current_price": 100.0,
        "prev_low": 92.0,
        "ma5": 103.0,
        "ma10": 98.0,
        "ma20": 95.0,
        "prior_high": 110.0,
        "recent_high": 108.0,
        "tech_signals": {"bollinger": {"lower": 90.0, "upper": 105.0}},
    }
    rendered = _key_levels(data)
    assert "支撑:MA10 98(-2.0%)" in rendered
    assert "压力:布林上轨 105(+5.0%)" in rendered


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
            "vote_label": "资金看空",
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

    assert "市场:防守(5.0)" not in rendered
    assert "2 方向:MA排列=N/A" in rendered
    assert "MACD状态=MACD死叉延续" in rendered
    assert "3 时机:RSI6=N/A(条件不可评)" in rendered
    assert "RSI14=30.3 触线(弱于40)" in rendered
    assert "4 量能:[盘口数据不足] 接口量比0.86@时间N/A(缩量;N/A)" in rendered
    assert "5 资金:两融=融资余额减少" in rendered
    assert "覆盖N/A/4｜票值-3" in rendered
    assert "6 买入:结论=技术投票偏空，未触发买入" in rendered
    assert "7 风控:观察｜跟踪=未触发" in rendered
    assert "技术面" not in rendered


def test_fund_details_stay_on_stock_cards():
    from src.push.templates import build_report_global_context

    first = {
        "stock_name": "第一股",
        "institutional_holding": {"votes": {
            "main_force": {"vote": 0, "detail": "对账源东财主力3日不可用，未完成双源复核"},
            "shareholder": {"vote": 0, "detail": "股东户数报告期20260630（报告期级滞后111天>90天）不参与投票"},
            "lhb": {"vote": 0, "detail": "近30日无龙虎榜记录"},
        }},
    }
    second = {
        "stock_name": "第二股",
        "institutional_holding": {"votes": {
            "main_force": {"vote": -1, "detail": "3日净流出"},
            "shareholder": {"vote": -1, "detail": "股东户数增加"},
            "lhb": {"vote": -1, "detail": "机构净卖出"},
        }},
    }
    context = build_report_global_context([first, second])

    assert not {item["label"] for item in context["items"]} & {"主力资金", "股东户数", "龙虎榜", "两融"}
    assert not context["funds"]


def test_sell_signal_uses_same_seven_question_reasoning_format():
    data = {
        "stock_name": "测试股",
        "stock_code": "600001",
        "current_price": 369.02,
        "change_pct": -2.22,
        "market_mode": "defend",
        "sector_name": "通信设备",
        "sector_status": "retreating",
        "exit_type": "技术走弱",
        "trigger_price": 369.02,
        "stop_loss_price": 317.54,
        "urgency": "重要",
        "reason": "[medium]布林下轨；[medium]偏空(-2.0)",
        "ma5": 380.0,
        "ma10": 390.0,
        "ma20": 400.0,
        "rsi": 39.4,
        "rsi6": 26.9,
        "bias6": -13.6,
        "volume_ratio": 1.41,
        "turnover_rate": 3.13,
        "tech_signals": {
            "category_votes": {
                "trend": {"vote": -1, "details": ["MACD死叉延续+绿柱扩大"]},
                "momentum": {"vote": 0, "details": ["RSI弱势(39)"]},
                "pattern": {"vote": 0, "details": ["K线偏涨但不足"]},
                "volume": {"vote": -1, "details": ["放量下跌"]},
            }
        },
        "institutional_holding": {
            "vote_score": -1,
            "vote_label": "资金偏空",
            "bullish_count": 0,
            "bearish_count": 1,
            "votes": {},
        },
    }

    _, rendered = render_exit_signal(data)

    assert "1 判读:" in rendered
    assert "2 方向:MA排列=MA空头排列" in rendered
    assert "MACD状态=MACD死叉延续+绿柱扩大" in rendered
    assert "5 资金:两融=N/A" in rendered
    assert "覆盖N/A/4｜票值-1" in rendered
    assert "7 风控:卖出｜跟踪=触发｜收盘确认价369.02｜止损:317.54" in rendered
    assert "技术面" not in rendered
    assert "退出逻辑" not in rendered


def test_timing_line_shows_threshold_gaps_and_discrete_hits():
    data = {
        "stock_name": "测试股",
        "stock_code": "600001",
        "change_pct": -2.0,
        "volume_ratio": 2.8,
        "rsi": 36.7,
        "rsi6": 29.5,
        "bias6": -5.1,
        "note": "买入: 时机口径测试 | 卖出: 未触发",
        "tech_signals": {
            "macd": {"state": "金叉延续"},
            "chan_divergence": {"type": "底背驰"},
        },
    }

    _, rendered = render_exit_signal(data)

    assert "3 时机:RSI6=29.5(超卖线25,还差4.5)" in rendered
    assert "RSI14=36.7 触线(弱于40)" in rendered
    assert "BIAS6=-5.1%(乖离佐证,不投票)" in rendered
    assert "离散=3/4(金叉|底背驰|缩量止跌|放量下破)" in rendered


def test_watch_ladder_reason_is_rendered_in_observation_card():
    data = {
        "stock_name": "大普微",
        "stock_code": "301666",
        "current_price": 100.0,
        "note": "买入: 策略检查:\n- 确认追强: 缺 ADX | 卖出: 止损未触发",
        "candidate_reason": "确认追强(当前模式可评估)：缺 ADX",
    }

    _, rendered = render_exit_signal(data)

    assert "规则=观察：确认追强(当前模式可评估)：缺 ADX" in rendered


def test_report_does_not_render_standalone_watch_ladder(monkeypatch):
    from src.push.pushplus import PushPlus

    captured = {}

    def _send(self, title, content, level="常规", **kwargs):
        captured.update({"title": title, "content": content})
        return True

    monkeypatch.setattr(PushPlus, "send", _send)
    PushPlus.__new__(PushPlus).send_intraday_report(
        {
            "market_mode": "defend",
            "market_score": 5.0,
            "watch_ladder": [{
                "stock_name": "大普微",
                "stock_code": "301666",
                "reason": "确认追强(当前模式可评估)：缺 ADX",
            }],
        },
        observations=[{
            "stock_name": "大普微",
            "stock_code": "301666",
            "current_price": 100.0,
            "note": "买入: 策略检查 | 卖出: 止损未触发",
            "candidate_reason": "确认追强(当前模式可评估)：缺 ADX",
        }],
    )

    assert "未触发候选" not in captured["content"]
    assert "未触发说明" not in captured["content"]
    assert "确认追强(当前模式可评估)：缺 ADX" in captured["content"]


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


def test_execution_plan_refreshes_stale_benchmark_for_rrr():
    """策略锚距现价超10%时，EV 必须用现价重算，禁止旧事件价格盲杀。"""
    plan = build_execution_plan(
        entry_type="恐慌抄底",
        benchmark_price=100.0,
        stop_loss=70.0,
        target_range=[110.0],
        tech_data={"current_price": 85.0},
    )

    assert plan.benchmark_source == "current_price_refresh"
    assert plan.strategy_benchmark_price == 100.0
    assert plan.benchmark_price == 85.0
    assert plan.rrr_low == pytest.approx((110.0 - 85.0) / (85.0 - 70.0))


def test_fundamental_line_discloses_upcoming_event_calendar():
    from src.push.templates import _fundamental_line

    rendered = _fundamental_line({
        "stock_code": "688630",
        "event_calendar": [{
            "date": "2026-10-28",
            "type": "财报",
            "warning_level": "中",
        }],
    })

    assert "未来事件:2026-10-28 财报(中" in rendered
