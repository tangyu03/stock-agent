"""
【四】策略配对出场 — 回归测试

每个策略在定义买入的那一刻就定义卖出：Z（认错）是 X 的直接否定（硬触发），
W（兑现）是价位触发（与进场同颗粒度）；旧漂移止损+投票门降级为辅助观察。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture(autouse=True)
def _no_institutional(monkeypatch):
    import os
    os.environ.setdefault("TQDM_DISABLE", "1")
    import src.analyzers.institutional_scorer as _inst
    monkeypatch.setattr(
        _inst, "score_institutional_holding",
        lambda c, *a, **kw: {"vote_score": 0, "vote_label": "skip",
                             "votes": {}, "bullish_count": 0,
                             "bearish_count": 0, "neutral_count": 4, "stale": False},
    )


def _kline(bars=60, base=95.0, vol=1_000_000):
    kline = []
    for i in range(bars):
        close = base + (i % 5) * 0.3
        kline.append({
            "date": f"2026-07-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + 1.0, "low": close - 1.0,
            "close": close, "volume": vol,
        })
    return kline


def _exit_tech(**overrides):
    data = {
        "current_price": 100.0,
        "ma5": 102.0, "ma10": 98.0, "ma20": 95.0,
        "ma5_prev": 101.0, "ma5_prev2": 100.0,
        "volume_ratio": 1.5,
        "kline": _kline(),
        "tech_signals": {},
    }
    data.update(overrides)
    return data


def _position(**overrides):
    """回执闭环的持仓（含入场假说）——价量突破，Y=89.5, Z=83.5, W=[96,102]"""
    data = {
        "log_id": 101,
        "entry_type": "价量突破",
        "paired_z": 83.5,
        "paired_w_low": 96.0,
        "paired_w_high": 102.0,
        "z_reference": 88.0,
        "entry_price": 89.5,
        "trigger_price": 89.5,
        "actual_price": 89.5,
        "hypothesis_sentence": "因为放量突破MA25，所以在89.50买入…",
    }
    data.update(overrides)
    return data


def _engine(monkeypatch, tech, position):
    from src.analyzers.timing_engine import TimingEngine
    te = TimingEngine(backtest_mode=False)
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
    if position is None:
        monkeypatch.setattr(te, "_get_paired_position", lambda code: None)
    else:
        monkeypatch.setattr(te, "_get_paired_position", lambda code: position)
    return te


class TestPairedExitZ:
    def test_paired_z_hard_trigger(self, monkeypatch):
        """现价跌破配对 Z（X 的直接否定）→ 破位止损硬触发，锚定 Z 而非漂移支撑"""
        tech = _exit_tech(current_price=83.0)         # < Z=83.5
        te = _engine(monkeypatch, tech, _position())
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        breakdown = [s for s in signals if s.exit_type == "破位止损"]
        assert len(breakdown) == 1
        assert breakdown[0].urgency == "紧急"
        assert "配对止损Z=83.50" in breakdown[0].reason
        assert "直接否定" in breakdown[0].reason

    def test_no_paired_position_keeps_legacy_anchor(self, monkeypatch):
        """无回执持仓（策略未知）→ 退回旧系统兜底（现价锚定支撑位）"""
        tech = _exit_tech(current_price=94.0)
        te = _engine(monkeypatch, tech, None)
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        for sig in signals:
            assert "配对止损Z" not in (sig.reason or "")


class TestPairedExitW:
    def test_paired_w_triggers_partial_take_profit(self, monkeypatch):
        """触及兑现位 W 低沿 → 策略兑现（减半+trailing），价位触发非投票门"""
        tech = _exit_tech(current_price=98.0)         # >= W低沿 96，< W高沿 102
        te = _engine(monkeypatch, tech, _position())
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        w_sig = [s for s in signals if s.exit_type == "策略兑现"]
        assert len(w_sig) == 1
        assert w_sig[0].source == "paired"
        assert w_sig[0].paired_strategy == "价量突破"
        assert "减半" in w_sig[0].reason
        assert "trailing" in w_sig[0].reason
        assert w_sig[0].urgency == "重要"

    def test_paired_w_high_triggers_full_exit(self, monkeypatch):
        """触及 W 高沿 → 清仓兑现（紧急）"""
        tech = _exit_tech(current_price=103.0)        # >= W高沿 102
        te = _engine(monkeypatch, tech, _position())
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        w_sig = [s for s in signals if s.exit_type == "策略兑现"]
        assert len(w_sig) == 1
        assert "清仓兑现" in w_sig[0].reason
        assert w_sig[0].urgency == "紧急"

    def test_momentum_exhaustion_w(self, monkeypatch):
        """确认追强特有 W：动能耗尽（强衰竭信号）→ 提前兑现"""
        tech = _exit_tech(current_price=99.0)         # 未触 W 价位
        tech["tech_signals"] = {
            "rsi": 85,                                   # 严重超买 → strong exhaustion
            "vote": "中性", "vote_score": 0,
        }
        position = _position(entry_type="确认追强", paired_w_low=105.0, paired_w_high=112.0)
        te = _engine(monkeypatch, tech, position)
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        w_sig = [s for s in signals if s.exit_type == "策略兑现"]
        assert len(w_sig) == 1
        assert "动能耗尽" in w_sig[0].reason
        assert w_sig[0].paired_strategy == "确认追强"


class TestLegacyExitDowngrade:
    def test_legacy_ma5_pressure_downgraded_to_observation(self, monkeypatch):
        """配对优先：旧 MA5 压制漂移止损降级为辅助观察（不再硬触发减半）"""
        # 构造 MA5 压制条件：多头排列 + MA5 上升 + 跌破 MA5 超阈值
        tech = _exit_tech(
            current_price=99.0,
            ma5=102.0, ma10=101.0, ma20=100.0,       # 多头排列
            ma5_prev=101.8, ma5_prev2=101.6,          # MA5 上升
        )
        # 现价 99 < MA5 102 → 跌破 2.94%，超过 max(1%, ATR 2%) 阈值 → 触发 MA5压制
        te = _engine(monkeypatch, tech, _position())
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        ma5_sig = [s for s in signals if s.exit_type == "MA5压制"]
        assert len(ma5_sig) == 1
        assert ma5_sig[0].urgency == "观察"            # 降级
        assert "辅助观察" in ma5_sig[0].reason
        assert "非策略配对出场" in ma5_sig[0].reason

    def test_breakdown_stays_hard_when_paired(self, monkeypatch):
        """破位止损作为系统安全网，配对时同样保持硬触发（不降观察）"""
        tech = _exit_tech(current_price=83.0)
        te = _engine(monkeypatch, tech, _position())
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        breakdown = [s for s in signals if s.exit_type == "破位止损"]
        assert len(breakdown) == 1
        assert breakdown[0].urgency in ("紧急", "重要")
        assert "辅助观察" not in breakdown[0].reason

    def test_legacy_unchanged_without_position(self, monkeypatch):
        """无持仓信息时旧系统出场逻辑保持原行为（不降级）"""
        tech = _exit_tech(
            current_price=99.0,
            ma5=102.0, ma10=101.0, ma20=100.0,
            ma5_prev=101.8, ma5_prev2=101.6,
        )
        te = _engine(monkeypatch, tech, None)
        signals = te.check_exit_signals("688028", "沃尔德", "defend")
        ma5_sig = [s for s in signals if s.exit_type == "MA5压制"]
        assert len(ma5_sig) == 1
        assert ma5_sig[0].urgency == "重要"            # 原级别
        assert "辅助观察" not in ma5_sig[0].reason
