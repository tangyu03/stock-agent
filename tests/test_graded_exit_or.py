"""
【Phase4 P0-3】卖出条件分级 OR — 回归测试

依据：两天 30+ 次卖出检查零触发，汇成真空 40 分钟 -6% 无响应、
-7.08% 仍判"未触发"——AND 门在数学上近乎永不开启的风控模块
等价于不存在。

决策细化（必须写进实现）：分级 OR——
  止损类（价格破 Z 线、技术走弱 medium 2/3）任一即出，不容商量；
  止盈类（冲高止盈、MA5 压制）保持原有计票。

验证：
  9/4 汇成真空回放应在 -6% 附近触发卖出；
  9/7 蘅东光"冲高止盈 strong 1/2"按分级规则继续持有。
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
    """小上影线 K 线（避免测试数据自带连续上影衰竭信号）"""
    kline = []
    for i in range(bars):
        close = base + (i % 5) * 0.3
        kline.append({
            "date": f"2026-08-{(i % 28) + 1:02d}",
            "open": close - 1.5, "high": close + 0.3, "low": close - 1.8,
            "close": close, "volume": vol,
        })
    return kline


def _huicheng_tech(**overrides):
    """汇成真空 9/4 回放形态：入场 192，现价 -6% ≈ 180.5"""
    tech = {
        "current_price": 180.5,
        "ma5": 191.0, "ma10": 188.0, "ma20": 185.0,
        "ma5_prev": 190.0, "ma5_prev2": 189.0,
        "volume_ratio": 1.6,
        "kline": _kline(base=190.0),
        "tech_signals": {
            "vote": "偏空",                # weakness medium 1
            "vote_score": -1.5,
        },
    }
    tech.update(overrides)
    return tech


def _paired_position(**overrides):
    """汇成真空持仓：价量突破，Z=结构位 183（P0-2 统一后裸结构位）"""
    data = {
        "log_id": 201,
        "entry_type": "价量突破",
        "paired_z": 183.0,
        "paired_w_low": 208.0,
        "paired_w_high": 215.0,
        "z_reference": 183.0,
        "entry_price": 192.0,
        "trigger_price": 192.0,
        "actual_price": 192.0,
        "hypothesis_sentence": "因为放量突破MA25，所以在192.00买入…",
    }
    data.update(overrides)
    return data


def _engine(monkeypatch, tech, position, graded_config=None):
    from src.analyzers.timing_engine import TimingEngine
    te = TimingEngine(backtest_mode=False)
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
    monkeypatch.setattr(te, "_get_paired_position", lambda code: position)
    if graded_config is not None:
        original_cfg = te._cfg
        def cfg_with(*path, default=None, _orig=original_cfg, _gc=graded_config):
            if path[:2] == ("exit", "graded_or"):
                key = path[2] if len(path) > 2 else ""
                return _gc.get(key, default)
            return _orig(*path, default=default)
        monkeypatch.setattr(te, "_cfg", cfg_with)
    return te


class TestStopClassGradedOR:
    """止损类任一即出，不容商量"""

    def test_missing_price_does_not_make_zero_stop_breakdown(self):
        """行情缺失时 0<=0 不是破位；不得生成无效卖出污染执行端。"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech()
        tech["current_price"] = 0
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        signals = te.check_exit_signals("301392", "汇成真空", "defend")

        assert signals == []
        assert te._exit_diagnostics["301392"] == "卖出检查: 现价缺失，数据不足，未评估"

    def test_huicheng_vacuum_minus6_triggers_z_break(self):
        """9/4 汇成真空回放：-6% 跌破 Z=183 → 破位止损（P0-2 裸结构位后 Z 贴近结构）"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech(current_price=180.5)   # 192 入场 → -6.0%
        te = te_mod.TimingEngine(backtest_mode=False)
        # 直接注入（无需 monkeypatch 引擎方法）
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        signals = te.check_exit_signals("301392", "汇成真空", "defend")
        breakdown = [s for s in signals if s.exit_type == "破位止损"]
        assert len(breakdown) == 1
        assert "配对止损Z=183.00" in breakdown[0].reason

    def test_technical_weakness_medium2_now_triggers(self):
        """技术走弱 medium 2/3（投票偏空 + 布林下轨）→ 分级 OR 止损类即出"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech()
        tech["tech_signals"] = {
            "vote": "偏空", "vote_score": -1.5,
            "bollinger": {"position": "below"},     # medium 2
        }
        # 现价不破 Z：隔离"技术走弱"分级 OR 通道
        tech["current_price"] = 190.0
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        signals = te.check_exit_signals("301392", "汇成真空", "defend")
        weakness = [s for s in signals if s.exit_type == "技术走弱"]
        assert len(weakness) == 1
        assert "分级OR·止损类" in weakness[0].reason
        assert "medium2/2" in weakness[0].reason

    def test_medium1_alone_still_does_not_trigger(self):
        """medium 1/2（单一走弱信号）不触发——不是把弱信号也变成卖出指令"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech()
        tech["tech_signals"] = {"vote": "偏空", "vote_score": -1.5}   # 仅 1 medium
        tech["current_price"] = 190.0
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        signals = te.check_exit_signals("301392", "汇成真空", "defend")
        assert not [s for s in signals if s.exit_type == "技术走弱"]

    def test_graded_or_off_restores_legacy_threshold(self):
        """回退开关：graded_or.enabled=false → 恢复 medium≥3 旧计票"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech()
        tech["tech_signals"] = {
            "vote": "偏空", "vote_score": -1.5,
            "bollinger": {"position": "below"},     # medium 2（旧阈值不够）
        }
        tech["current_price"] = 190.0
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        original_cfg = te._cfg
        te._cfg = lambda *path, default=None, _o=original_cfg: (
            False if path[:2] == ("exit", "graded_or") and (len(path) < 3 or path[2] == "enabled")
            else _o(*path, default=default)
        )
        signals = te.check_exit_signals("301392", "汇成真空", "defend")
        assert not [s for s in signals if s.exit_type == "技术走弱"]

    def test_exit_diagnostics_text_reflects_graded_threshold(self):
        """观察卡 ⑥风控 文案：medium X/2·分级OR止损类（透明可审计）"""
        import src.analyzers.timing_engine as te_mod
        tech = _huicheng_tech()
        tech["current_price"] = 190.0
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position()
        te.check_exit_signals("301392", "汇成真空", "defend")
        diagnostics = te._exit_diagnostics.get("301392", "")
        assert "medium" in diagnostics and "/2·分级OR止损类" in diagnostics


class TestProfitClassKeepsVoteCounting:
    """止盈类（冲高止盈、MA5 压制）保持原有计票——防止 OR 化摆到频繁误杀的另一极"""

    def _hengdongguang_tech(self):
        """蘅东光 9/7：现价 530.77 +14.81%，冲高止盈 strong 1/2（观察区持仓视角）"""
        return {
            "current_price": 530.77,
            "ma5": 505.0, "ma10": 480.0, "ma20": 455.0,
            "ma5_prev": 500.0, "ma5_prev2": 495.0,
            "volume_ratio": 2.01,
            "kline": _kline(base=480.0),
            "tech_signals": {
                # strong 1：RSI 严重超买；medium 1：RSI 高位
                "rsi": 82.0,
                "vote": "偏多", "vote_score": 1.5,
            },
        }

    def test_strong_1_of_2_keeps_holding(self):
        """冲高止盈 strong 1/2 → 不触发卖出（计票保留），按分级规则继续持有"""
        import src.analyzers.timing_engine as te_mod
        tech = self._hengdongguang_tech()
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        # 蘅东光为价量突破持仓（W 兑现区在上方，未触及）
        te._get_paired_position = lambda code: _paired_position(
            entry_type="价量突破", paired_z=455.0, paired_w_low=560.0,
            paired_w_high=580.0, z_reference=455.0, entry_price=480.0,
        )
        signals = te.check_exit_signals("920045", "蘅东光", "defend")
        # 无破位（530>455）、无走弱（偏多）、冲高止盈 strong 1 < 2 → 不出
        assert not [s for s in signals if s.exit_type == "冲高止盈"]
        assert not [s for s in signals if s.exit_type == "破位止损"]
        assert not [s for s in signals if s.exit_type == "技术走弱"]

    def test_ma5_pressure_still_requires_full_and_set(self):
        """MA5 压制保持三条件 AND（多头排列+MA5上升+跌破阈值）"""
        import src.analyzers.timing_engine as te_mod
        tech = self._hengdongguang_tech()
        # 构造多头排列+MA5上升，但价格在 MA5 上方 → 不触发
        te = te_mod.TimingEngine(backtest_mode=False)
        te._fetch_tech_data = lambda code, mode="defend": tech
        te._get_paired_position = lambda code: _paired_position(
            entry_type="价量突破", paired_z=455.0, paired_w_low=560.0,
            paired_w_high=580.0, z_reference=455.0, entry_price=480.0,
        )
        signals = te.check_exit_signals("920045", "蘅东光", "defend")
        assert not [s for s in signals if s.exit_type == "MA5压制"]


class TestSensitivityStats:
    """作废条件数据：周触发>5次 → 回调结构位距离参数（而非回退 AND）"""

    def test_graded_exit_sensitivity_counts_stop_class(self, monkeypatch):
        from datetime import date, timedelta
        import src.feedback.strategy_stats as stats_mod
        recent_date = date.today().isoformat()
        old_date = (date.today() - timedelta(days=30)).isoformat()
        rows = [
            {"exit_date": recent_date, "stock_code": "301392", "exit_type": "破位止损"},
            {"exit_date": recent_date, "stock_code": "300308", "exit_type": "技术走弱"},
            {"exit_date": recent_date, "stock_code": "688028", "exit_type": "冲高止盈"},  # 止盈类不计
            {"exit_date": old_date, "stock_code": "002975", "exit_type": "破位止损"},     # 出窗不计
        ]

        class _FakeCursor:
            def execute(self, *a, **kw):
                return self
            def fetchall(self):
                return [dict(r) for r in rows]

        class _FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def cursor(self):
                return _FakeCursor()

        monkeypatch.setattr(stats_mod, "get_conn", lambda: _FakeConn())
        report = stats_mod.graded_exit_sensitivity(days=7)
        assert report["week_triggers"] == 2
        assert "未超5次" in report["note"]

    def test_over_five_triggers_recalibration_note(self, monkeypatch):
        from datetime import date
        import src.feedback.strategy_stats as stats_mod
        rows = [
            {"exit_date": date.today().isoformat(), "stock_code": f"30000{i}",
             "exit_type": "破位止损"} for i in range(6)
        ]

        class _FakeCursor:
            def execute(self, *a, **kw):
                return self
            def fetchall(self):
                return [dict(r) for r in rows]

        class _FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def cursor(self):
                return _FakeCursor()

        monkeypatch.setattr(stats_mod, "get_conn", lambda: _FakeConn())
        report = stats_mod.graded_exit_sensitivity(days=7)
        assert report["week_triggers"] == 6
        assert "回调结构位距离参数" in report["note"]
        assert "而非回退AND" in report["note"]
