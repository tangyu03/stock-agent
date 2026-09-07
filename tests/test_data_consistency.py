"""
【二】数据层一致性校验 — 回归测试

科创板"股被当手"两条最便宜的拦截规则：
  规则1: 量比 < 1 而量能倍数 > 10 → 9/3 全部 4 条误触发应被拦下
  规则2: 换手 < 10% 而成交量 > 10 亿股 → 9/4 沃尔德应被拦下
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.signal_plan import build_volume_snapshot, DATA_GUARD_DEFAULTS


def _kline(bars=80, vol=1_000_000, turnover=4.0):
    kline = []
    for i in range(bars):
        close = 100.0 + (i % 5) * 0.3
        kline.append({
            "date": f"2026-06-{(i % 28) + 1:02d}",
            "close": close, "volume": vol, "turnover_rate": turnover,
        })
    return kline


class TestVolumeConsistencyRules:
    def test_rule1_volume_ratio_conflict_marks_dirty(self):
        """9/3 场景：量比 0.8（真实缩量）而量能倍数 15（今日量被放大百倍）"""
        snapshot = build_volume_snapshot({
            "kline": _kline(vol=1_000_000),
            "today_volume": 15_000_000_000,   # 被放大的今日量（股/手错位）
            "volume_ratio": 0.8,
        })
        assert snapshot.dirty is True
        assert "量能口径冲突" in snapshot.dirty_reason
        assert snapshot.label == "量能脏数据"

    def test_rule2_turnover_volume_conflict_marks_dirty(self):
        """9/4 沃尔德场景：换手 4.8% 而成交量 13 亿股（流通盘撑不起）"""
        snapshot = build_volume_snapshot({
            "kline": _kline(vol=1_000_000, turnover=4.8),
            "today_volume": 1_300_000_000,    # 13 亿股
            "volume_ratio": 2.0,              # 量比正常 → 规则1 不触发
            "turnover_rate": 4.8,             # 换手 < 10%
        })
        assert snapshot.dirty is True
        assert "换手/成交量口径冲突" in snapshot.dirty_reason
        assert "4.8" in snapshot.dirty_reason

    def test_rule2_requires_both_conditions(self):
        """高换手 + 天量（如次新巨量换手）不算脏；低换手 + 常量也不算脏"""
        # 高换手 + 大量 → 不脏
        snapshot = build_volume_snapshot({
            "kline": _kline(vol=1_000_000, turnover=25.0),
            "today_volume": 1_300_000_000,
            "volume_ratio": 2.0,
            "turnover_rate": 25.0,
        })
        assert snapshot.dirty is False
        # 低换手 + 正常量 → 不脏
        snapshot2 = build_volume_snapshot({
            "kline": _kline(vol=1_000_000, turnover=4.0),
            "today_volume": 2_000_000,
            "volume_ratio": 2.0,
            "turnover_rate": 4.0,
        })
        assert snapshot2.dirty is False

    def test_rule2_threshold_overridable(self):
        """守卫阈值可由 config data_guard 覆盖"""
        snapshot = build_volume_snapshot(
            {
                "kline": _kline(vol=1_000_000, turnover=4.0),
                "today_volume": 600_000_000,     # 6 亿股，默认不触发
                "volume_ratio": 2.0,
                "turnover_rate": 4.0,
            },
            guard={"max_volume_shares": 5e8},     # 阈值收紧到 5 亿
        )
        assert snapshot.dirty is True

    def test_normal_data_not_dirty(self):
        snapshot = build_volume_snapshot({
            "kline": _kline(vol=1_000_000),
            "today_volume": 2_000_000,
            "volume_ratio": 1.8,
            "turnover_rate": 5.0,
        })
        assert snapshot.dirty is False
        assert snapshot.dirty_reason == ""
        assert snapshot.data_ok is True


class TestGuardDefaults:
    def test_defaults_match_spec(self):
        """用户规格：换手阈值 10%，量级阈值 10 亿股"""
        assert DATA_GUARD_DEFAULTS["turnover_threshold_pct"] == 10.0
        assert DATA_GUARD_DEFAULTS["max_volume_shares"] == 1.0e9


class TestDirtyBlocksSignalGeneration:
    """脏数据在 check_entry_signals 生成阶段即拦截（不留到推送）"""

    def test_wald_day2_volume_conflict_blocks_entry(self, monkeypatch):
        import os
        os.environ.setdefault("TQDM_DISABLE", "1")
        import src.analyzers.institutional_scorer as _inst
        monkeypatch.setattr(
            _inst, "score_institutional_holding",
            lambda c, *a, **kw: {"vote_score": 0, "vote_label": "skip",
                                 "votes": {}, "bullish_count": 0,
                                 "bearish_count": 0, "neutral_count": 4, "stale": False},
        )
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()

        kline = [
            {"date": "2026-08-01", "close": 100.0, "volume": 1_000_000, "turnover_rate": 4.0}
            for _ in range(80)
        ]
        tech_data = {
            "kline": kline,
            "current_price": 101.01,
            "today_volume": 1_300_000_000,     # 13 亿股
            "volume_ratio": 2.0,
            "turnover_rate": 4.8,              # 换手 4.8% → 规则2 触发
            "ma25": 88.0, "ma25_prev": 88.5, "prev_close": 88.2,
        }
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech_data)

        signals = te.check_entry_signals("688028", "沃尔德", "defend")
        assert signals == []
        assert te._tech_data_full["688028"]["volume_data_valid"] is False
        assert "换手/成交量口径冲突" in te._tech_data_full["688028"]["entry_blocked_reason"]
