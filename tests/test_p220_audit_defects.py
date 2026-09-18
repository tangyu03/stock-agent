# -*- coding: utf-8 -*-
"""P2-20 新缺陷回归：A-E（卖出口径自相矛盾/早盘豁免/维持口径/闸门预期/同期同期重复词）。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os
os.environ["TQDM_DISABLE"] = "1"

import src.analyzers.institutional_scorer as _inst
_inst.score_institutional_holding = lambda c, *args, **kwargs: {
    "vote_score": 0, "vote_label": "skip", "votes": {},
    "bullish_count": 0, "bearish_count": 0, "neutral_count": 4, "stale": False,
}

from src.analyzers.timing_engine import get_backtest_timing_engine
from src.analyzers.volume_pattern import _inject_ratio_caliber, build_volume_pattern
from src.analyzers.signal_lifecycle import render_entry_and_maintenance
from src.push.templates import _mode_gate_expectation, render_environment_overview


def _vp_early_data(**overrides):
    data = {
        "change_pct": 1.0, "volume_ratio": 2.0, "turnover_rate": 5.0,
        "current_price": 100.0, "ma5": 101.0, "ma10": 102.0, "ma20": 103.0,
        "prior_high": 104.0, "recent_high": 104.0, "gain_20d": 0.05, "ma20_falling": False,
        "tech_signals": {
            "order_flow": {"available": True, "outer_volume": 1200, "inner_volume": 800, "imbalance_pct": 20.0},
            "volume_snapshot": {
                "is_early_window": True,
                "same_period_volume_ratio": 2.51,
                "same_period_caliber": "同期累计量比(5日同期,U曲线校准)",
                "same_period_sample_time": "20260918103100",
                "volume_ratio_caliber": "接口量比(5日分钟均量)",
                "volume_ratio_sample_time": "20260918103100",
            },
        },
    }
    data.update(overrides)
    return data


def _exit_tech(kline, **overrides):
    return {
        "kline": kline,
        "current_price": 11.5,
        "ma5": 11.0, "ma10": 10.8, "ma20": 10.5,
        "ma5_prev": 10.9, "ma5_prev2": 10.8,
        "volume_ratio": 2.51,
        "tech_signals": {
            "vote": "中性", "vote_score": 0,
            "rsi": 85.0, "rsi6": 85.0,
            "kdj": {"k": 50, "d": 50, "j": 50},
            "macd": {"dif": 0.1, "dea": 0.05},
            "volume_snapshot": {
                "is_early_window": True,
                "same_period_volume_ratio": 2.51,
                "same_period_caliber": "同期累计量比(5日同期,U曲线校准)",
                "same_period_sample_time": "20260918101100",
            },
        },
        "kline_pattern": [],
        "institutional_holding": {},
        **overrides,
    }


class TestDefectA_CaliberDisclosure:
    """A: 卖出量价背离必须披露口径，且与量能组放量口径并存时显式标注冲突。"""

    def test_量价背离标签带口径与冲突标注(self):
        te = get_backtest_timing_engine()
        vols = [100.0] * 5 + [120.0, 130.0, 140.0, 150.0, 70.0]
        kline = [{"开盘": 10 + i * 0.1, "最高": 11 + i * 0.1, "最低": 9.8 + i * 0.1,
                  "收盘": 10.5 + i * 0.1, "成交量": v} for i, v in enumerate(vols)]
        te._fetch_tech_data = lambda code, mode: _exit_tech(kline)
        te._get_paired_position = lambda code: None
        signals = te.check_exit_signals("688820", "盛合晶微", "defend")
        hit = [s.reason for s in signals if "量价背离" in s.reason]
        assert hit, "应生成量价背离卖出信号"
        assert "较5日均量0.70x" in hit[0]
        assert "另同期量比2.51x为放量口径" in hit[0]


class TestDefectB_EarlyExemption:
    """B: 早盘量能用于卖出时显式豁免说明（卖出风控从宽/买入确认从严）。"""

    def test_早盘量能上下文带豁免说明(self):
        te = get_backtest_timing_engine()
        ctx = te._volume_context({
            "tech_signals": {"volume_snapshot": {
                "is_early_window": True,
                "same_period_volume_ratio": 2.51,
                "same_period_caliber": "同期累计量比(5日同期,U曲线校准)",
                "same_period_sample_time": "20260918101100",
            }},
        })
        assert "早盘量能仅作卖出风控从宽计入(买入确认从严)" in ctx


class TestDefectC_MaintenanceCaliber:
    """C: 维持面板量比必须带口径（沃尔德 4.69 vs 同期1.57 并存无口径）。"""

    def test_维持面板带口径标注(self):
        class _E:
            born_date = "2026-09-17"
            entry_snapshot = "{}"
            maintenance_check = (
                '{"date":"2026-09-18","volume_ratio":4.69,"is_early_window":true,'
                '"same_period_volume_ratio":1.57,'
                '"same_period_caliber":"同期累计量比(5日同期,U曲线校准)",'
                '"volume_ratio_caliber":"接口量比(5日分钟均量)",'
                '"ma_alignment":true,"rsi":57.0,"sector_status":"轮动"}'
            )
        note = render_entry_and_maintenance(_E())
        assert "维持(今日): 量比1.57(同期累计量比(5日同期,U曲线校准))" in note

    def test_旧快照无口径保持原格式(self):
        class _E:
            born_date = "2026-09-17"
            entry_snapshot = "{}"
            maintenance_check = (
                '{"date":"2026-09-18","volume_ratio":0.8,'
                '"ma_alignment":true,"rsi":57.0,"sector_status":"轮动"}'
            )
        note = render_entry_and_maintenance(_E())
        assert "维持(今日): 量比0.80 △缩量" in note
        assert "量比0.80(" not in note


class TestDefectD_GateExpectation:
    """D: 环境栏加一行闸门预期（今日若收盘站回MA5，明日转进攻）。"""

    def test_防守模式有闸门预期(self):
        assert _mode_gate_expectation("defend") == "今日若收盘站回MA5，明日转进攻"
        assert _mode_gate_expectation("retreat") == "今日若收盘收复MA5×0.99，明日转防守"
        assert _mode_gate_expectation("attack") == ""

    def test_环境总览渲染闸门预期(self):
        html = render_environment_overview({"market_mode": "defend", "market_score": 5.0})
        assert "闸门预期:今日若收盘站回MA5，明日转进攻" in html
        html_attack = render_environment_overview({"market_mode": "attack", "market_score": 8.0})
        assert "闸门预期" not in html_attack


class TestDefectE_DuplicateWord:
    """E: “同期同期”重复词——确认项已带口径前缀时不重复注入。"""

    def test_注入函数不重复前缀(self):
        assert _inject_ratio_caliber("同期量比确认方向", "同期量比@09-18 10:31 ") == "同期量比@09-18 10:31 确认方向"
        assert _inject_ratio_caliber("量比回升1.2+", "同期量比@09-18 10:31 ") == "同期量比@09-18 10:31 回升1.2+"
        assert _inject_ratio_caliber("10:30后复核量能", "同期量比@09-18 10:31 ") == "10:30后复核量能"

    def test_早盘观察确认条件无重复词(self):
        res = build_volume_pattern(_vp_early_data())
        joined = " ".join(res.get("confirms") or [])
        assert "同期同期" not in joined
        assert "同期量比@09-18 10:31 确认方向" in joined
