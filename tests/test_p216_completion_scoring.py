# -*- coding: utf-8 -*-
"""P2-16 后验自动记分：完结事件自动记分，按策略/分型/位置三维累计。

验收标准：完结事件自动记分（命中+1/方向对节奏错+0.5/未验证0/失败-1），
按策略分型位置三维累计，累计样本 >= 10 时自动输出偏差方向；
报告尾部出现累计分型命中率区块。
"""
import json


def _ev(status, reason="", entry_type="价量突破", pattern="放量突破", position="低位"):
    return {
        "status": status,
        "invalid_reason": reason,
        "entry_type": entry_type,
        "entry_snapshot": json.dumps({"pattern": pattern, "position": position}),
    }


def test_score_completed_event_all_statuses():
    from src.feedback.completion_scoring import score_completed_event

    assert score_completed_event("sig_target") == (1.0, "命中")
    assert score_completed_event("sig_stop") == (-1.0, "失败")
    assert score_completed_event("chase_abandon") == (0.5, "方向对节奏错")
    # invalidated：止损/跌破为失败，否则未验证
    assert score_completed_event("invalidated", "跌破结构位") == (-1.0, "失败")
    assert score_completed_event("invalidated", "止盈离场") == (0.0, "未验证")
    assert score_completed_event("invalidated", "") == (0.0, "未验证")
    # time_exit / expired：未验证
    assert score_completed_event("time_exit") == (0.0, "未验证")
    assert score_completed_event("expired") == (0.0, "未验证")
    # 未知状态兜底
    assert score_completed_event("unknown") == (0.0, "未验证")
    assert score_completed_event(None, None) == (0.0, "未验证")


def test_aggregate_completion_scores_three_dimensions():
    from src.feedback.completion_scoring import aggregate_completion_scores

    events = [
        _ev("sig_target", entry_type="价量突破", pattern="放量突破", position="低位"),
        _ev("sig_stop", entry_type="价量突破", pattern="放量突破", position="低位"),
        _ev("chase_abandon", entry_type="均线回踩", pattern="缩量回踩", position="高位"),
        _ev("time_exit", entry_type="均线回踩", pattern="缩量回踩", position="高位"),
    ]
    agg = aggregate_completion_scores(events)
    assert agg["total"]["n"] == 4
    assert abs(agg["total"]["score"] - (1.0 - 1.0 + 0.5 + 0.0)) < 1e-9

    # 策略维度
    vp = agg["dims"]["策略"]["价量突破"]
    assert vp["n"] == 2
    assert vp["命中"] == 1
    assert vp["失败"] == 1
    assert abs(vp["score"]) < 1e-9

    # 分型维度
    fp = agg["dims"]["分型"]["放量突破"]
    assert fp["n"] == 2
    assert fp["失败"] == 1

    # 位置维度
    pos = agg["dims"]["位置"]["低位"]
    assert pos["n"] == 2

    # 缺失维度归"未记录"
    ev_none = {"status": "time_exit", "invalid_reason": "", "entry_type": "", "entry_snapshot": None}
    agg2 = aggregate_completion_scores([ev_none])
    assert agg2["dims"]["策略"]["未记录"]["n"] == 1
    assert agg2["dims"]["分型"]["未记录"]["n"] == 1
    assert agg2["dims"]["位置"]["未记录"]["n"] == 1


def test_deviation_directions_requires_10_samples():
    from src.feedback.completion_scoring import aggregate_completion_scores, deviation_directions

    # 少于 10 样本：不输出偏差
    small = [_ev("sig_target") for _ in range(9)]
    agg_small = aggregate_completion_scores(small)
    assert deviation_directions(agg_small, small) == []

    # 10 样本但命中率整体 100%：无明显偏差维度
    good = [_ev("sig_target") for _ in range(10)]
    agg_good = aggregate_completion_scores(good)
    assert deviation_directions(agg_good, good) == []

    # 命中率显著低于整体（>=10pp）的维度输出偏差方向
    events = []
    # 整体：8 命中 2 失败 = 80%
    for _ in range(4):
        events.append(_ev("sig_target", pattern="放量突破", position="低位"))
    for _ in range(4):
        events.append(_ev("sig_target", pattern="缩量回踩", position="低位"))
    events.append(_ev("sig_stop", pattern="放量突破", position="低位"))
    events.append(_ev("sig_stop", pattern="缩量回踩", position="低位"))
    # 位置：低位 8 命中 2 失败 80%
    for _ in range(4):
        events.append(_ev("sig_target", pattern="放量突破", position="高位"))
    for _ in range(4):
        events.append(_ev("sig_stop", pattern="放量突破", position="高位"))
    # 位置：高位 4 命中 4 失败 50%，低于整体 60%，gap 10pp -> 偏差
    agg = aggregate_completion_scores(events)
    dev = deviation_directions(agg, events)
    assert any("位置「高位」" in line for line in dev)


def test_render_completion_score_block_markers():
    from src.feedback.completion_scoring import render_completion_score_block

    events = [_ev("sig_target"), _ev("sig_stop"), _ev("chase_abandon")]
    block = render_completion_score_block(events)
    assert "累计分型命中率" in block
    assert "总样本3" in block
    assert "按策略" in block
    assert "按分型" in block
    assert "按位置" in block

    # 无事件：提示待样本积累，不抛异常
    empty = render_completion_score_block([])
    assert "暂无完结事件" in empty
