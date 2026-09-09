"""
【Phase4 P0-1】量能外推口径 — 回归测试（决策记录验证项）

蘅东光 9/7 的 1.02x 拦截是直接实证——盘中累计量对比全天均量，
午前结构性小于 1 是数学必然（分子只走了半天），与标的强弱无关；
外推口径下同一数据为 2.0x。回放应从拦截变为触发。
"""
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.analyzers.volume_projection import (
    DEFAULT_U_CURVE, u_fraction_at, project_volume_ratio,
    InMemoryProjectionStore, set_projection_store, record_projection,
    finalize_day_projection, projection_error_report,
)


def _at(time_str: str) -> datetime:
    # 2000-01-03 是周一，规避周末判定
    return datetime.strptime(f"2000-01-03 {time_str}", "%Y-%m-%d %H:%M")


class TestUCurve:
    def test_fraction_at_1127_matches_halfday_evidence(self):
        """11:27 的 U 分位 ≈ 0.51（午前半天实证口径），1.02x → 外推 2.0x"""
        fraction = u_fraction_at(_at("11:27"))
        assert 0.48 <= fraction <= 0.54

    def test_fraction_is_monotonic_within_day(self):
        points = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
        fractions = [u_fraction_at(_at(p)) for p in points]
        assert fractions == sorted(fractions)

    def test_lunch_break_uses_morning_close_fraction(self):
        assert u_fraction_at(_at("12:30")) == u_fraction_at(_at("11:30"))

    def test_after_close_is_full_day(self):
        """收盘后（含夜间/周末）累计量即全天量"""
        assert u_fraction_at(_at("15:30")) == 1.0
        assert u_fraction_at(_at("09:00")) == 1.0


class TestProjectionGuards:
    """反方对冲：10:00 前禁用、封板禁用、14:45 后退化——已内置"""

    def test_pre_market_window_disabled(self):
        result = project_volume_ratio(1.0, change_pct=2.0, now=_at("09:45"))
        assert result.mode == "pre_window"
        assert result.projected_ratio is None
        assert "开盘冲量" in result.note

    def test_limit_locked_disabled(self):
        """封板禁用：涨停缩量惜售/跌停恐慌下量能分布失真"""
        result = project_volume_ratio(1.0, change_pct=10.0, now=_at("11:00"), limit_ratio=0.10)
        assert result.mode == "limit_locked"
        assert result.projected_ratio is None

    def test_limit_down_locked_too(self):
        result = project_volume_ratio(1.0, change_pct=-20.0, now=_at("13:30"), limit_ratio=0.20)
        assert result.mode == "limit_locked"

    def test_post_window_degrades_to_cumulative(self):
        """14:45 后退化为累计量（已接近全天量，外推无增益）"""
        result = project_volume_ratio(0.95, change_pct=2.0, now=_at("14:50"))
        assert result.mode == "post_window"
        assert result.projected_ratio == 0.95

    def test_config_off_returns_none(self):
        result = project_volume_ratio(1.0, change_pct=2.0, now=_at("11:00"),
                                      config={"enabled": False})
        assert result.mode == "config_off"

    def test_hengdongguang_102_at_1127_projects_to_2x(self):
        """蘅东光 9/7 实证：1.02x@11:27 → 外推口径 2.0x
        （北交所 920045 涨跌停 30%，+14.81% 非封板，外推可用）"""
        result = project_volume_ratio(1.02, change_pct=14.81, now=_at("11:27"),
                                      limit_ratio=0.30)
        assert result.mode == "ok"
        assert 1.9 <= result.projected_ratio <= 2.1
        assert "外推口径" in result.note

    def test_bj_limit_30pct_not_locked_at_15pct_gain(self):
        """封板判定的板块差异：北交所 +14.81%（<30%）不锁，主板 +10%（≥10%）锁"""
        bj = project_volume_ratio(1.0, change_pct=14.81, now=_at("11:00"), limit_ratio=0.30)
        assert bj.mode == "ok"
        mainboard = project_volume_ratio(1.0, change_pct=10.0, now=_at("11:00"), limit_ratio=0.10)
        assert mainboard.mode == "limit_locked"


class TestSnapshotIntegration:
    """build_volume_snapshot 集成：快照携带外推口径"""

    def test_snapshot_carries_projection(self):
        from src.analyzers.signal_plan import build_volume_snapshot
        kline = [{"volume": 1_000_000, "turnover_rate": 5.0}] * 60
        tech = {
            "kline": kline,
            "today_volume": 1_020_000,
            "volume_ma60": 1_000_000,
            "volume_ratio": 2.01,
            "change_pct": 14.81,
            "projection_time": "11:27",   # 回放注入（实盘用当前时钟）
            "projection_limit_ratio": 0.30,  # 北交所 30%（蘅东光 920045）
        }
        snapshot = build_volume_snapshot(tech)
        assert snapshot.volume_vs_ma60 == pytest.approx(1.02, abs=0.01)
        assert snapshot.projected_volume_vs_ma60 is not None
        assert snapshot.projected_volume_vs_ma60 >= 1.9
        assert snapshot.projection_mode == "ok"

    def test_wld_092_projects_to_breakout(self):
        """沃尔德 9/7：0.92x@11:27 → 外推 1.8x ≥ 1.2 阈值 → 量能确认成立"""
        from src.analyzers.signal_plan import build_volume_snapshot
        tech = {
            "kline": [{"volume": 1_000_000, "turnover_rate": 5.0}] * 60,
            "today_volume": 920_000,
            "volume_ma60": 1_000_000,
            "volume_ratio": 1.72,
            "change_pct": 7.02,
            "projection_time": "11:27",
        }
        snapshot = build_volume_snapshot(tech)
        assert snapshot.volume_vs_ma60 < 1.0                      # 实际口径拦截
        assert snapshot.projected_volume_vs_ma60 >= 1.2           # 外推口径触发


class TestBreakoutTriggerWithProjection:
    """蘅东光 9/7 类形态回放：价量突破从拦截变为触发"""

    def _engine(self):
        from src.analyzers.timing_engine import TimingEngine
        return TimingEngine(backtest_mode=True)

    def _breakout_tech(self, today_volume, **overrides):
        kline = []
        for i in range(70):
            close = 88.0 + (i % 10) * 0.2
            kline.append({
                "date": f"2026-08-{(i % 28) + 1:02d}",
                "open": close - 0.4, "high": close + 1.2, "low": close - 1.2,
                "close": close, "volume": 1_000_000, "turnover_rate": 5.0,
            })
        tech = {
            "current_price": 89.5, "today_open": 88.8,
            "ma25": 88.0, "ma25_prev": 88.5, "prev_close": 88.2,
            "ma5": 88.8, "ma10": 88.6, "ma20": 87.5,
            "volume_ratio": 2.0, "turnover_rate": 4.0,
            "kline": kline,
            "today_volume": today_volume, "volume_ma60": 1_000_000,
            "recent_high": 92.0,
            "change_pct": 3.5,
            "projection_time": "11:27",          # 蘅东光 9/7 类回放时点
        }
        tech.update(overrides)
        return tech

    def test_intraday_low_cumulative_now_triggers_via_projection(self):
        """累计 0.92x < 1.0（旧口径拦截）→ 外推 1.8x ≥ 1.2 → 触发"""
        te = self._engine()
        tech = self._breakout_tech(today_volume=920_000)
        te._projection_cfg = lambda: {}  # 回测模式默认禁用外推——显式打开以回放实盘口径
        from src.analyzers.timing_engine import StopLossCalc
        stop = StopLossCalc(
            stock_code="920045", current_price=89.5,
            support_candidates=[], chosen_support=88.0,
            stop_loss_price=87.0, resistance=92.0,
        )
        sig = te._check_volume_breakout("920045", "蘅东光", tech, stop, "defend", "main_trend")
        assert sig is not None
        assert "外推口径" in sig.trigger_reason

    def test_projection_off_keeps_intercept(self):
        """外推关闭（回测口径）→ 0.92x 拦截如旧（开关可回退）"""
        te = self._engine()
        tech = self._breakout_tech(today_volume=920_000)
        from src.analyzers.timing_engine import StopLossCalc
        stop = StopLossCalc(
            stock_code="920045", current_price=89.5,
            support_candidates=[], chosen_support=88.0,
            stop_loss_price=87.0, resistance=92.0,
        )
        sig = te._check_volume_breakout("920045", "蘅东光", tech, stop, "defend", "main_trend")
        assert sig is None                      # 回测模式：日频数据不外推

    def test_raw_threshold_still_works_without_projection(self):
        """实际口径 >1.0 的常规触发路径不受外推影响"""
        te = self._engine()
        from src.analyzers.signal_plan import build_volume_snapshot
        tech = self._breakout_tech(today_volume=1_500_000, projection_time=None)
        from src.analyzers.timing_engine import StopLossCalc
        stop = StopLossCalc(
            stock_code="000001", current_price=89.5,
            support_candidates=[], chosen_support=88.0,
            stop_loss_price=87.0, resistance=92.0,
        )
        # 非交易时段（测试进程时间）外推=累计，走实际口径分支
        sig = te._check_volume_breakout("000001", "测试", tech, stop, "defend", "main_trend")
        if datetime.now().weekday() < 5 and 10 <= datetime.now().hour < 15:
            pytest.skip("交易时段运行，时钟相关跳过")
        assert sig is not None


class TestErrorLedger:
    """验证项：每日收盘比对“外推全天量 vs 实际全天量”，误差进记录表"""

    def test_record_and_finalize_computes_error(self):
        store = InMemoryProjectionStore()
        set_projection_store(store)
        try:
            record_projection("920045", "蘅东光", raw_ratio=1.02, projected_ratio=2.0,
                              sample_time="11:27", trade_date="2026-09-07")
            updated = finalize_day_projection({"920045": 1.85}, trade_date="2026-09-07")
            assert updated == 1
            rows = store.fetch_days(10)
            assert rows[0]["actual_ratio"] == 1.85
            assert rows[0]["error_pct"] == pytest.approx(abs(2.0 - 1.85) / 1.85 * 100, rel=0.01)

            report = projection_error_report()
            assert report["samples"] == 1
            assert not report["persistent_over_alert"]
        finally:
            set_projection_store(None)

    def test_persistent_error_triggers_recalibration_note(self):
        """作废条件：10:30 后误差持续 >15% → 换标的池分位数表（框架保留，参数重校）"""
        store = InMemoryProjectionStore()
        set_projection_store(store)
        try:
            for i in range(4):
                code = f"30000{i}"
                record_projection(code, f"标的{i}", raw_ratio=1.0, projected_ratio=2.4,
                                  sample_time="13:00", trade_date=f"2026-09-0{i + 1}")
                finalize_day_projection({code: 1.0}, trade_date=f"2026-09-0{i + 1}")
            report = projection_error_report()
            assert report["persistent_over_alert"] is True
            assert "参数重校" in report["note"]
        finally:
            set_projection_store(None)

    def test_pre_1030_samples_excluded(self):
        """10:30 前的样本不计入误差判定（开盘冲量天然高估，不作数）"""
        store = InMemoryProjectionStore()
        set_projection_store(store)
        try:
            record_projection("000001", "标的", raw_ratio=1.0, projected_ratio=3.0,
                              sample_time="09:45", trade_date="2026-09-07")
            finalize_day_projection({"000001": 1.0}, trade_date="2026-09-07")
            report = projection_error_report()
            assert report["samples"] == 0
        finally:
            set_projection_store(None)
