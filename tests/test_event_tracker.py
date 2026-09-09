"""
【Phase4 P1-3】昨日事件追踪表 — 回归测试

依据：9/3 的 6 条信号在 9/4 全灭，系统没有任何回头看的行为——
没有记忆的系统无法校准任何参数。

验证：下一份收盘版顶部出现表格；9/3→9/4 的六条信号有闭合记录
（状态：了结，浮亏 -3~-8%）。
"""
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.feedback.event_tracker import build_event_tracking_table, render_event_tracking_table


_SIX_SIGNALS_9_3 = [
    # 9/3 六条信号 → 9/4 全灭（状态：了结，浮亏 -3~-8%）
    {"stock_code": "301392", "stock_name": "汇成真空", "strategy": "价量突破",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -5.2,
     "zw_triggered": 1, "exit_type": "破位止损"},
    {"stock_code": "688028", "stock_name": "沃尔德", "strategy": "价量突破",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -3.1,
     "zw_triggered": 1, "exit_type": "破位止损"},
    {"stock_code": "300308", "stock_name": "中际旭创", "strategy": "确认追强",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -4.4,
     "zw_triggered": 0, "exit_type": "技术走弱"},
    {"stock_code": "688409", "stock_name": "富创精密", "strategy": "套利低吸",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -7.9,
     "zw_triggered": 1, "exit_type": "破位止损"},
    {"stock_code": "688041", "stock_name": "海光信息", "strategy": "价量突破",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -3.8,
     "zw_triggered": 0, "exit_type": "MA5压制"},
    {"stock_code": "300843", "stock_name": "胜蓝股份", "strategy": "价量突破",
     "trigger_date": "2026-09-03", "exit_date": "2026-09-04", "pnl_pct": -6.5,
     "zw_triggered": 1, "exit_type": "破位止损"},
]


class TestClosedRecordChain:
    """9/3→9/4 六条信号的闭合记录（决策记录验证项）"""

    def test_six_signals_have_closed_records(self):
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), lookback_days=2,
            closed_trades=_SIX_SIGNALS_9_3, events=[],
        )
        assert len(rows) == 6
        closed = [r for r in rows if r["status"] == "了结"]
        assert len(closed) == 6
        # 浮亏区间 -3~-8%（决策记录原文口径）
        losses = [float(r["outcome"].replace("浮亏", "").replace("%", "")) for r in closed]
        assert min(losses) <= -3.0 and max(losses) >= -7.0
        assert all(-9.0 <= v <= -3.0 for v in losses)

    def test_rows_link_entry_to_exit(self):
        """链路字段：入场日→离场日 + 触发类型（止损→再评估的链路基础）"""
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), closed_trades=_SIX_SIGNALS_9_3, events=[],
        )
        huicheng = next(r for r in rows if r["stock_code"] == "301392")
        assert "2026-09-03入场→2026-09-04离场" in huicheng["link"]
        assert "破位止损" in huicheng["link"]
        assert "Z触发(认错)" in huicheng["link"]

    def test_outside_window_excluded(self):
        """窗口外（>2日）的了结不进表"""
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 7), lookback_days=2,
            closed_trades=_SIX_SIGNALS_9_3, events=[],
        )
        assert rows == []          # 9/3-9/4 相对 9/7 已出窗（近2日=9/5起）

    def test_render_table_for_daily_review_top(self):
        """渲染：收盘版顶部的表格文本（含闭合记录与月度校准提醒）"""
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), closed_trades=_SIX_SIGNALS_9_3, events=[],
        )
        text = render_event_tracking_table(rows)
        assert "汇成真空(301392)" in text
        assert "了结" in text
        assert "浮亏" in text
        assert "每月一次按表校准参数" in text            # 反方对冲：记录了更要分析

    def test_empty_table_has_honest_placeholder(self):
        text = render_event_tracking_table([])
        assert "无信号事件与了结记录" in text
        assert "记录闭环从第一笔信号开始积累" in text


class TestEventLifecycleRows:
    """事件状态迁移（未入场的撤单/过期也进表——审计链不只覆盖成交）"""

    def test_invalidated_event_appears_with_reason(self):
        events = [{
            "event_id": "evt-20260904113000-920045-确认追强",
            "stock_code": "920045", "stock_name": "蘅东光",
            "entry_type": "确认追强",
            "born_date": "2026-09-04",
            "status": "invalidated",
            "invalid_reason": "收盘跌回突破位，假说被证伪",
        }]
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), events=events, closed_trades=[],
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "失效撤单"
        assert "假说被证伪" in rows[0]["outcome"]
        assert "诞生2026-09-04" in rows[0]["link"]

    def test_closed_trade_takes_priority_over_same_event(self):
        """同一事件已有闭合记录 → 生命周期行去重（一笔交易一行）"""
        events = [{
            "event_id": "evt-x1", "stock_code": "301392", "stock_name": "汇成真空",
            "entry_type": "价量突破", "born_date": "2026-09-03",
            "status": "triggered", "invalid_reason": "",
        }]
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), events=events, closed_trades=_SIX_SIGNALS_9_3,
        )
        assert len([r for r in rows if r["stock_code"] == "301392"]) == 1
        assert rows[0]["status"] == "了结"

    def test_open_events_optional(self):
        """待回踩的活跃事件默认展示，include_open=False 可关闭"""
        events = [{
            "event_id": "evt-x2", "stock_code": "000001", "stock_name": "平安银行",
            "entry_type": "价量突破", "born_date": "2026-09-05",
            "status": "valid", "invalid_reason": "",
        }]
        rows_on = build_event_tracking_table(
            as_of=date(2026, 9, 5), events=events, closed_trades=[])
        rows_off = build_event_tracking_table(
            as_of=date(2026, 9, 5), events=events, closed_trades=[],
            include_open=False)
        assert len(rows_on) == 1 and rows_on[0]["status"] == "待回踩"
        assert len(rows_off) == 0

    def test_signal_event_objects_supported(self):
        """SignalEvent 对象（内存 store 的元素）也可直接注入"""
        from src.analyzers.signal_lifecycle import SignalEvent
        event = SignalEvent(
            event_id="evt-x3", stock_code="002975", stock_name="博杰股份",
            entry_type="价量突破", born_date="2026-09-05",
            expire_date="2026-09-10", breakout_level=99.39,
            entry_price=102.77, stop_loss=99.39,
            status="valid",
        )
        rows = build_event_tracking_table(
            as_of=date(2026, 9, 5), events=[event], closed_trades=[])
        assert len(rows) == 1
        assert rows[0]["stock_code"] == "002975"
        assert rows[0]["status"] == "待回踩"


class TestDailyReviewTop:
    """收盘版顶部出现表格（generate 集成，离线安全）"""

    def test_daily_review_starts_with_event_table(self, monkeypatch):
        from src.feedback.daily_review import DailyReview
        review = DailyReview.__new__(DailyReview)   # 绕过网络依赖的 __init__
        review._aggregator = None
        review._pushplus = None

        class _StubLogger:
            def get_today_logs(self):
                return []
            def get_current_holdings(self):
                return []
        review._logger = _StubLogger()

        monkeypatch.setattr(
            "src.feedback.event_tracker.build_event_tracking_table",
            lambda **kw: build_event_tracking_table(
                as_of=date(2026, 9, 5), closed_trades=_SIX_SIGNALS_9_3, events=[]),
        )
        report = review.generate()
        head = report.splitlines()[:8]
        assert any("昨日事件追踪" in line for line in head)      # 顶部出现表格
        assert any("了结" in line for line in report.splitlines()[:15])
        assert "参数附录" in report                              # P3-3 附录在底部
