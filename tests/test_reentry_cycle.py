"""
【Phase4 P1-2】再入场循环 — 回归测试

依据：趋势跟踪的盈利结构依赖"小止损+再入场+让利润奔跑"
（蘅东光 9/4 止损 -5%、9/7 +14.8%，缺的只是第二次入场）。
9/3 六条信号 9/4 全灭后无一重新评估——止损在系统里等于死刑判决。

纪律（反方对冲）：再入场必须是新事件（创新高或收复 Z 线，
且重新通过完整触发条件），绝不基于沉没成本；次数上限 2 次。

验证：蘅东光 9/7 创新高时应出现在待命队列头部而非观察区中部；
9/4 止损记录与 9/7 再评估记录在事件追踪表中形成链路。
"""
import sys
from datetime import date, timedelta
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


def _store_with_prior_events(events):
    from src.analyzers.signal_lifecycle import InMemorySignalEventStore, SignalEvent
    store = InMemorySignalEventStore()
    for event_id, born, status, stop_loss, entry_type in events:
        store.save(SignalEvent(
            event_id=event_id,
            stock_code="920045",
            stock_name="蘅东光",
            entry_type=entry_type,
            born_date=born,
            expire_date=(date.fromisoformat(born) + timedelta(days=5)).isoformat(),
            breakout_level=480.0,
            entry_price=490.0,
            stop_loss=stop_loss,
            target_low=560.0,
            target_high=580.0,
            status=status,
            invalid_reason="" if status != "invalidated" else "收盘跌回突破位，假说被证伪",
        ))
    return store


class TestReentryStatus:
    """再入场待命判定：新事件（创新高/收复Z线）驱动"""

    def test_new_high_puts_stock_in_standby_head(self):
        """蘅东光 9/7 创新高 → 待命队列头部（决策记录验证项）"""
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 481.0, "确认追强"),  # 9/4 止损
        ])
        status = reentry_status(
            "920045", current_price=530.77, recent_high=528.0,
            max_reentries=2, store=store,
        )
        assert status["standby"] is True
        assert status["reason"] == "创新高"
        assert "再入场待命·头部" in status["note"]
        assert "2026-09-04" in status["note"]          # 9/4 止损记录与 9/7 再评估形成链路

    def test_z_line_recovery_puts_stock_in_standby(self):
        """收复 Z 线：价格重新站上上次认错位 → 待命（结构修复）"""
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 481.0, "确认追强"),
        ])
        status = reentry_status(
            "920045", current_price=490.0, recent_high=560.0,   # 未创新高但 > 481
            max_reentries=2, store=store,
        )
        assert status["standby"] is True
        assert status["reason"] == "收复Z线"
        assert "收复上次认错位481.00" in status["note"]

    def test_below_z_and_below_high_not_standby(self):
        """既未创新高也未收复 Z → 再入场条件未激活（不基于沉没成本）"""
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 481.0, "确认追强"),
        ])
        status = reentry_status(
            "920045", current_price=450.0, recent_high=560.0,
            max_reentries=2, store=store,
        )
        assert status["standby"] is False
        assert status["reason"] == "新事件未成立"

    def test_attempts_exhausted(self):
        """次数用尽：2 次尝试后 → 转入长期观察（真死刑）"""
        from src.analyzers.signal_lifecycle import reentry_status, reentry_exhausted
        store = _store_with_prior_events([
            ("evt-0903", "2026-09-03", "invalidated", 475.0, "确认追强"),
            ("evt-0905", "2026-09-05", "invalidated", 478.0, "价量突破"),
        ])
        status = reentry_status(
            "920045", current_price=530.0, recent_high=528.0,
            max_reentries=2, store=store,
        )
        assert status["standby"] is False
        assert status["reason"] == "次数用尽"
        assert "转入长期观察" in status["note"]
        assert reentry_exhausted("920045", 2, store=store) is True

    def test_triggered_event_is_not_reentry_candidate(self):
        """已成交事件不得进入再入队列——同一 evt_id 不能同时说买和持有。"""
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0908", "2026-09-08", "triggered", 481.0, "价量突破"),
        ])
        status = reentry_status(
            "920045", current_price=530.77, recent_high=528.0,
            max_reentries=2, store=store,
        )
        assert status["standby"] is False
        assert status["reason"] == "已成交事件在跟踪"
        assert status["event_id"] == "evt-0908"
        assert "不进入再入场队列" in status["note"]

    def test_reentry_only_counts_failed_terminal_events(self):
        from src.analyzers.signal_lifecycle import reentry_exhausted
        store = _store_with_prior_events([
            ("evt-0901", "2026-09-01", "triggered", 470.0, "价量突破"),
            ("evt-0903", "2026-09-03", "invalidated", 475.0, "价量突破"),
        ])
        assert reentry_exhausted("920045", 2, store=store) is False

    def test_one_attempt_not_exhausted(self):
        from src.analyzers.signal_lifecycle import reentry_exhausted
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 481.0, "确认追强"),
        ])
        assert reentry_exhausted("920045", 2, store=store) is False

    def test_no_history_not_standby(self):
        from src.analyzers.signal_lifecycle import reentry_status
        status = reentry_status(
            "920045", current_price=530.0, recent_high=528.0,
            max_reentries=2, store=_store_with_prior_events([]),
        )
        assert status["standby"] is False

    def test_expired_events_do_not_count_as_attempts(self):
        """纯过期（未入场）不算尝试次数——次数只数真实尝试"""
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0901", "2026-09-01", "expired", 470.0, "价量突破"),
            ("evt-0902", "2026-09-02", "expired", 470.0, "价量突破"),
        ])
        status = reentry_status(
            "920045", current_price=530.0, recent_high=528.0,
            max_reentries=2, store=store,
        )
        # expired（未入场形态）不计 attempts → 无尝试可再入，
        # 正常信号流程覆盖该标的（再入场队列只服务"止损后再走强"）
        assert status["attempts"] == 0
        assert status["standby"] is False
        assert status["reason"] == "无已了结尝试"


class TestReentryGateInEngine:
    """check_entry_signals 的再入场次数上限（拒绝留痕）"""

    def _engine_with_store(self, monkeypatch, store, tech):
        from src.analyzers.timing_engine import TimingEngine, StopLossCalc
        from src.analyzers.signal_lifecycle import SignalLifecycle
        te = TimingEngine(backtest_mode=True)
        te._lifecycle = SignalLifecycle(store, valid_days=5)
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        return te

    def _breakout_tech(self):
        kline = []
        for i in range(70):
            close = 88.0 + (i % 10) * 0.2
            kline.append({
                "date": f"2026-08-{(i % 28) + 1:02d}",
                "open": close - 0.4, "high": close + 1.2, "low": close - 1.2,
                "close": close, "volume": 1_000_000, "turnover_rate": 5.0,
            })
        return {
            "current_price": 89.5, "today_open": 88.8,
            "ma25": 88.0, "ma25_prev": 88.5, "prev_close": 88.2,
            "ma5": 88.8, "ma10": 88.6, "ma20": 87.5,
            "volume_ratio": 2.0, "turnover_rate": 4.0,
            "kline": kline,
            "today_volume": 2_000_000, "volume_ma60": 1_000_000,
            "recent_high": 92.0,
            "change_pct": 3.5,
            "tech_signals": {"vote_score": 2.0},
            "institutional_holding": {"vote_score": 0, "votes": {}},
        }

    def test_exhausted_code_yields_no_new_signal(self, monkeypatch):
        store = _store_with_prior_events([
            ("evt-0903", "2026-09-03", "invalidated", 87.5, "价量突破"),
            ("evt-0905", "2026-09-05", "invalidated", 87.5, "价量突破"),
        ])
        te = self._engine_with_store(monkeypatch, store, self._breakout_tech())
        signals = te.check_entry_signals("920045", "蘅东光", "defend", "main_trend")
        assert signals == []
        rejection = te._entry_rejections.get("920045")
        assert rejection is not None
        assert rejection["entry_type"] == "再入场上限"
        assert any("再入场次数用尽" in r for r in rejection["reasons"])

    def test_one_prior_attempt_still_allows_new_signal(self, monkeypatch):
        """一次尝试后新事件（突破条件重新满足）→ 信号照常诞生（再入场放行）"""
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 87.0, "价量突破"),
        ])
        te = self._engine_with_store(monkeypatch, store, self._breakout_tech())
        signals = te.check_entry_signals("920045", "蘅东光", "defend", "main_trend")
        assert len(signals) == 1
        assert signals[0].entry_type == "价量突破"
        assert signals[0].event_id                      # 新事件诞生（链路第二环）


class TestSunkCostForbidden:
    """绝不基于"已经亏了所以补回"的沉没成本逻辑"""

    def test_note_explicitly_disclaims_sunk_cost(self):
        from src.analyzers.signal_lifecycle import reentry_status
        store = _store_with_prior_events([
            ("evt-0904", "2026-09-04", "invalidated", 481.0, "确认追强"),
        ])
        status = reentry_status(
            "920045", current_price=530.77, recent_high=528.0, store=store,
        )
        # 待命文案显式声明：不携带沉没成本逻辑，待完整触发条件重新确认
        assert "不携带沉没成本逻辑" in status["note"]
        assert "完整触发条件" in status["note"]
