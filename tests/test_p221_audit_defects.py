# -*- coding: utf-8 -*-
"""
【P2-21】09-18 审计缺陷闭环 — 回归测试

本轮用户审计（沃尔德 9-18）落地的三项程序修改：

P0-1 事件冻结语义披露（signal_lifecycle）：
  冻结 = "停提示 + 挂单存续（解冻后恢复可成交）"，而非默认撤单；
  冻结/解冻 trigger_data 携带 price，状态机每次转换写日志（时间+价格+触发条件）。

P0-2 报告内状态一致性（signal_lifecycle + unified_engine）：
  正文诊断"事件状态:"行在入场循环生成，早于出场循环的 freeze/unfreeze/
  mark_filled——直接渲染会与头部在飞栏（渲染时点读库最新状态）矛盾
  （沃尔德：正文"已冻结"而头部"已成交"）。修复：
    1) 冻结持续期间 invalid_reason 随快照滚动更新（禁止展示两个版本前旧原因）；
    2) 统一引擎出场循环后对仍带事件状态行的诊断重渲染（同源同时点）。

P1-3 换手对账基准披露（volume_pattern）：
  换手分档改为"相对自身 N 日分位"（臻宝 16.20% 绝对档=决战，相对自身是缩量），
  分位缺失/不单调/全 0 时退化为绝对档；渲染与 result 均披露基准。
"""
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.signal_lifecycle import (
    InMemorySignalEventStore,
    SignalLifecycle,
)
from src.analyzers.volume_pattern import (
    build_volume_pattern,
    render_volume_pattern,
    turnover_gear_relative,
)


def _make_event(entry_price=498.0, stop_loss=480.0):
    lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
    event = lifecycle.register_event(
        "920045", "蘅东光", "确认追强",
        breakout_level=490.0, entry_price=entry_price,
        stop_loss=stop_loss, target_low=560.0, target_high=580.0,
    )
    return lifecycle, event


# ============================================================
# P0-1 冻结语义披露 + 价格入日志
# ============================================================

def test_freeze_reason_discloses_pending_order_survives():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "frozen"
    assert "冻结仅停提示、挂单存续" in saved.invalid_reason


def test_freeze_log_trigger_data_carries_price():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    logs = lifecycle.store.get_event_logs(event.event_id)
    freeze_logs = [log for log in logs if log["rule_entry"] == "R4.1事件冻结"]
    assert freeze_logs
    assert freeze_logs[-1]["trigger_data"].get("price") == 499.0


def test_frozen_event_does_not_fill_even_when_low_touches_entry():
    """冻结=停提示+挂单存续：冻结期间即使日内最低价触及买点也不成交。"""
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    lifecycle.evaluate_events(
        "920045", current_price=499.0, day_low=480.0,
        tech_score=-1.5, volume_ratio=1.2, change_pct=-2.0,
        today=date(2026, 9, 10),
    )
    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "frozen"
    assert "已冻结" in lifecycle.event_status_note("920045", current_price=499.0)


# ============================================================
# P0-2 冻结原因滚动 + 状态一致性刷新
# ============================================================

def test_frozen_reason_rolls_forward_with_latest_snapshot():
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    lifecycle.evaluate_events(
        "920045", current_price=497.0,
        tech_score=-1.0, volume_ratio=1.4, change_pct=-3.1,
        today=date(2026, 9, 10),
    )
    saved = lifecycle.store.events[event.event_id]
    assert saved.status == "frozen"
    assert "量比1.40" in saved.invalid_reason
    assert "量比1.30" not in saved.invalid_reason
    roll_logs = [
        log for log in lifecycle.store.get_event_logs(event.event_id)
        if log["rule_entry"] == "R3.11冻结快照滚动"
    ]
    assert roll_logs, "缺少冻结快照滚动留痕"
    assert roll_logs[-1]["trigger_data"]["price"] == 497.0
    assert roll_logs[-1]["from_status"] == "frozen"
    assert roll_logs[-1]["to_status"] == "frozen"


def test_unfreeze_status_note_shows_current_not_stale_frozen():
    """沃尔德 9-18 核心缺陷：正文显示"已冻结"而头部已解冻/成交。"""
    lifecycle, event = _make_event()
    lifecycle.evaluate_events(
        "920045", current_price=499.0,
        tech_score=-2.0, volume_ratio=1.3, change_pct=-5.2,
        today=date(2026, 9, 9),
    )
    stale_note = lifecycle.event_status_note("920045", current_price=499.0)
    assert "已冻结" in stale_note
    # 出场循环解冻
    lifecycle.evaluate_events(
        "920045", current_price=500.0,
        tech_score=0.5, volume_ratio=1.0, change_pct=1.0,
        today=date(2026, 9, 10),
    )
    # 统一引擎 2b 刷新：正文诊断的事件状态行重渲染为当前状态
    diagnostics = "策略未触发(动量条件未满足)\n事件状态: " + stale_note
    marker = "\n事件状态: "
    prefix = diagnostics.split(marker, 1)[0]
    fresh_note = lifecycle.event_status_note("920045", current_price=505.0)
    refreshed = prefix + marker + fresh_note
    assert "已冻结" not in refreshed
    assert "买点上方待回踩" in refreshed


# ============================================================
# P1-3 换手分档相对分位基准披露
# ============================================================

def _pattern_data(turnover=16.2, **overrides):
    data = {
        "stock_name": "臻宝", "stock_code": "688000",
        "current_price": 123.0, "change_pct": 1.0,
        "volume_ratio": 1.5, "turnover_rate": turnover,
        "ma5": 120.0, "ma10": 118.0, "ma20": 115.0,
        "prior_high": 130.0, "recent_high": 128.0,
        "tech_signals": {
            "volume_snapshot": {
                "volume_ratio": 1.5, "volume_ratio_effective": 1.5,
                "is_early_window": False,
                "turnover_rate": turnover,
                "turnover_p25": 5.0, "turnover_p50": 10.0,
                "turnover_p75": 15.0, "turnover_p90": 20.0,
                "turnover_hot": False,
            },
            "order_flow": {
                "available": True,
                "outer_volume": 1200, "inner_volume": 800,
                "imbalance_pct": 20.0,
            },
        },
    }
    data.update(overrides)
    return data


def test_turnover_gear_uses_relative_percentile_base():
    result = build_volume_pattern(_pattern_data())
    assert result["tgear_base"] == "相对自身N日分位"
    assert result["tgear"] == "过热"
    rendered = " ".join(render_volume_pattern(result))
    assert "基准:相对自身N日分位" in rendered


def test_turnover_gear_falls_back_to_absolute_without_percentiles():
    data = _pattern_data()
    del data["tech_signals"]["volume_snapshot"]["turnover_p25"]
    del data["tech_signals"]["volume_snapshot"]["turnover_p90"]
    result = build_volume_pattern(data)
    assert result["tgear_base"] == "绝对档"
    rendered = " ".join(render_volume_pattern(result))
    assert "基准:绝对档" in rendered


def test_turnover_gear_relative_boundaries_and_degeneration():
    assert turnover_gear_relative(3.0, 5, 10, 15, 20) == "清淡"
    assert turnover_gear_relative(8.0, 5, 10, 15, 20) == "正常"
    assert turnover_gear_relative(12.0, 5, 10, 15, 20) == "活跃"
    assert turnover_gear_relative(16.2, 5, 10, 15, 20) == "过热"
    assert turnover_gear_relative(25.0, 5, 10, 15, 20) == "决战"
    # 分位缺失 / 不单调 / 全 0 / 输入缺失 → 退化绝对档
    assert turnover_gear_relative(16.2, None, 10, 15, 20) is None
    assert turnover_gear_relative(16.2, 20, 10, 15, 20) is None
    assert turnover_gear_relative(16.2, 0, 0, 0, 0) is None
    assert turnover_gear_relative(None, 5, 10, 15, 20) is None
