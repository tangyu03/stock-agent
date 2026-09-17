"""
【三】信号事件生命周期 — 回归测试（状态 → 事件）

"站上MA25"是状态，今天为真、明天也为真，于是沃尔德连发两天买入信号。
事件化后：突破当日诞生（收阳+量能双腿）→ N 日内回踩买点有效 →
收盘跌回突破位/板块退潮立即撤单 → 超期作废。昨天的信号不再原样重播。
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


def _kline(bars=80, base=90.0, vol=1_000_000):
    kline = []
    for i in range(bars):
        close = base + (i % 7) * 0.4
        kline.append({
            "date": f"2026-06-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + 1.5, "low": close - 1.5,
            "close": close, "volume": vol, "amount": vol * close,
        })
    return kline


def _breakout_day_tech(**overrides):
    """9/3 突破当日：昨收 88.2 在昨日 MA25(88.5) 下方，今价 89.5 站上今日 MA25(88.0)
    均线排列为突破场景：MA10(88.6) 在 MA25(88.0) 上方（上升趋势中的回踩突破）"""
    data = {
        "current_price": 89.5,
        "today_open": 88.8,              # 收阳：current > open
        "ma25": 88.0, "ma25_prev": 88.5, "prev_close": 88.2,
        "ma5": 88.8, "ma10": 88.6, "ma20": 87.5,
        "volume_ratio": 2.0, "turnover_rate": 4.0,
        "kline": _kline(base=88.0),
        "today_volume": 2_000_000, "volume_ma60": 1_000_000,
        "recent_high": 92.0,
    }
    data.update(overrides)
    return data


def _day2_tech(**overrides):
    """9/4 已站上后的第二天：昨收 89.5 已在昨日 MA25 上方 → 非突破当日"""
    data = _breakout_day_tech(
        current_price=101.01, today_open=90.0,
        ma25=88.5, ma25_prev=88.0, prev_close=89.5,
        ma10=94.0, ma5=97.0, ma20=92.0,
    )
    data.update(overrides)
    return data


class TestBreakoutEventBoundary:
    def test_signal_born_on_breakout_day(self, monkeypatch):
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)

        signals = te.check_entry_signals("688028", "沃尔德", "defend")
        assert len(signals) == 1
        sig = signals[0]
        assert sig.entry_type == "价量突破"
        assert sig.hypothesis.get("z", 0) < sig.hypothesis.get("y", 0)   # Z < Y
        assert sig.event_id                                   # 事件已注册
        # 事件已进入生命周期（born → valid）
        events = te._lifecycle.get_active_events("688028")
        assert len(events) == 1
        assert events[0].entry_price == sig.hypothesis["y"]
        assert events[0].stop_loss == sig.hypothesis["z"]

    def test_no_replay_on_second_day(self, monkeypatch):
        """9/4 沃尔德场景：事件边界拦下（昨收已在昨日MA25上方，非突破当日）"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()      # 全新引擎 → 无活跃事件，纯测事件边界
        tech = _day2_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)

        signals = te.check_entry_signals("688028", "沃尔德", "defend")
        assert signals == []                   # "站上MA25"状态不再重播

    def test_active_event_blocks_regeneration(self, monkeypatch):
        """同日重复扫描：活跃事件去重（不会再原样重播）"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)

        first = te.check_entry_signals("688028", "沃尔德", "defend")
        assert len(first) == 1
        second = te.check_entry_signals("688028", "沃尔德", "defend")
        assert second == []                     # 同一事件生命周期内不重发

    def test_active_filled_event_blocks_new_entry_generation(self, monkeypatch):
        """价量突破已成交后，恐慌抄底等新信号不得同标的双份建仓。"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)

        first = te.check_entry_signals("688028", "沃尔德", "defend")
        assert len(first) == 1
        te._lifecycle.mark_filled(first[0].event_id)
        assert te._lifecycle.get_active_events("688028")[0].status == "filled"

        second = te.check_entry_signals("688028", "沃尔德", "defend")
        assert second == []

    def test_event_boundary_can_be_disabled(self, monkeypatch):
        """require_event_boundary=False → 回到旧"状态"行为（留给回溯对照）"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine(
            params_override={"volume_breakout": {
                "require_event_boundary": False, "require_bullish_close": False,
            }}
        )
        tech = _day2_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        signals = te.check_entry_signals("688028", "沃尔德", "defend")
        assert len(signals) == 1                # 旧行为：状态为真即发

    def test_no_bullish_close_no_birth(self, monkeypatch):
        """突破当日收阴（现价<今开）→ 诞生双腿缺一不可"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech(current_price=88.1, today_open=88.8)
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        signals = te.check_entry_signals("688028", "沃尔德", "defend")
        assert signals == []


class TestLifecycleInvalidation:
    def test_close_back_below_breakout_invalidates_event(self, monkeypatch):
        """收盘跌回突破位 → 立即撤单（信号作废通知）"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        te.check_entry_signals("688028", "沃尔德", "defend")

        # 次日跌回突破位 88.0 下方
        notices = te.evaluate_signal_events("688028", current_price=86.5, sector_status="rotational")
        assert len(notices) == 1
        assert notices[0]["exit_type"] == "信号作废"
        assert "回踩失败已撤单" in notices[0]["reason"]
        assert "撤单" in notices[0]["reason"]
        assert te._lifecycle.get_active_events("688028") == []   # 已失效

    def test_sector_retreating_invalidates_event(self, monkeypatch):
        """板块状态机转为退潮 → 立即撤单"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        te.check_entry_signals("688028", "沃尔德", "defend")

        notices = te.evaluate_signal_events("688028", current_price=90.0, sector_status="retreating")
        assert len(notices) == 1
        assert "退潮" in notices[0]["reason"]
        assert te._lifecycle.get_active_events("688028") == []

    def test_retreating_sector_does_not_block_valid_entry_signal(self, monkeypatch):
        """退潮只拦已持仓加仓；空仓标的的入场信号仍按策略条件生成。"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)

        signals = te.check_entry_signals(
            "688099", "测试", "defend", sector_status="retreating"
        )
        assert len(signals) == 1
        assert signals[0].entry_type == "价量突破"

    def test_daily_close_state_machine(self):
        """日内低点触及 Y 且收盘守住 Y → 成交；收盘破 Y → 回踩撤单。"""
        from src.analyzers.signal_lifecycle import (
            InMemorySignalEventStore,
            SignalLifecycle,
        )
        store = InMemorySignalEventStore()
        lifecycle = SignalLifecycle(store, valid_days=5)
        event = lifecycle.register_event(
            "688028", "沃尔德", "价量突破",
            breakout_level=88.0, entry_price=89.50, stop_loss=83.50,
            target_low=96.0, target_high=102.0,
            hypothesis={"x": "放量突破MA25"},
        )
        notices = lifecycle.evaluate_events(
            "688028", current_price=90.0, day_high=91.0,
            day_low=89.40, close_price=90.10,
        )
        assert event.status == "filled"
        assert "虚拟成交" in notices[0]["reason"]

        lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
        failed = lifecycle.register_event(
            "688029", "博杰", "价量突破",
            breakout_level=88.0, entry_price=102.77, stop_loss=91.44,
            target_low=112.89, target_high=120.21,
            hypothesis={"x": "放量突破MA25"},
        )
        notices = lifecycle.evaluate_events(
            "688029", current_price=100.91, day_high=104.0,
            day_low=101.50, close_price=100.91,
        )
        assert failed.status == "invalidated"
        assert "回踩失败已撤单" in notices[0]["reason"]

    def test_event_expires_after_valid_days(self, monkeypatch):
        """N 日内回踩买点有效，超期作废"""
        from src.analyzers.timing_engine import get_backtest_timing_engine
        from src.analyzers.signal_lifecycle import SignalLifecycle, InMemorySignalEventStore
        store = InMemorySignalEventStore()
        lifecycle = SignalLifecycle(store, valid_days=5)
        event = lifecycle.register_event(
            stock_code="688028", stock_name="沃尔德", entry_type="价量突破",
            breakout_level=88.0, entry_price=89.5, stop_loss=83.5,
            target_low=96.0, target_high=102.0, hypothesis={"x": "放量突破MA25"},
        )
        # 第 6 天 → 过期
        future = date.fromisoformat(event.born_date) + timedelta(days=6)
        notices = lifecycle.evaluate_events(
            "688028", current_price=90.0, sector_status="rotational", today=future
        )
        assert len(notices) == 1
        assert notices[0]["exit_type"] == "信号过期"
        assert "作废" in notices[0]["reason"]

    def test_status_note_shows_validity(self, monkeypatch):
        from src.analyzers.timing_engine import get_backtest_timing_engine
        te = get_backtest_timing_engine()
        tech = _breakout_day_tech()
        monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
        te.check_entry_signals("688028", "沃尔德", "defend")
        note = te.lifecycle_status_note("688028", current_price=89.0)
        assert "价量突破" in note
        assert "回踩买点有效" in note or "买点上方待回踩" in note


class TestAudienceRouting:
    """【三】受众：买入事件只对空仓者成立；持仓者输出四选一"""

    def _entry(self, code="688028", sector_status=""):
        return {
            "stock_code": code,
            "stock_name": "沃尔德",
            "entry_type": "价量突破",
            "trigger_price": 89.5,
            "confidence": "高",
            "hypothesis": {
                "x": "放量突破MA25", "y": 89.5, "z": 83.5, "w": [96.0, 102.0],
                "sentence": "因为放量突破MA25，所以在89.50买入…",
            },
            "execution_plan": {
                "execute": True,
                "benchmark_price": 89.5,
                "execution_tiers": [{"role": "main", "price": 89.5}],
            },
            "sector_status": sector_status,
        }

    def test_held_stock_gets_position_advice_not_buy(self):
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            [self._entry()], [],
            holdings=[{"code": "688028", "stock_name": "沃尔德",
                       "shares": 1000, "cost_price": 85.0}],
        )
        assert scheduled["buy"] == []                          # 不再当空仓者推销
        assert len(scheduled["position_advice"]) == 1
        advice = scheduled["position_advice"][0]
        assert advice.audience == "holding"
        assert advice.position_action == "加仓"                # 四选一
        assert "假说" in advice.schedule_note

    def test_held_retreating_sector_becomes_no_add(self):
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            [self._entry(sector_status="retreating")], [],
            holdings=[{"code": "688028", "stock_name": "沃尔德",
                       "shares": 1000, "cost_price": 85.0}],
        )
        assert scheduled["buy"] == []
        assert len(scheduled["position_advice"]) == 1
        advice = scheduled["position_advice"][0]
        assert advice.audience == "holding"
        assert advice.position_action == "不加仓"
        assert "板块退潮，禁止加仓" in advice.schedule_note

    def test_empty_retreating_sector_gets_normal_buy(self):
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            [self._entry(sector_status="retreating")], [],
            holdings=[],
        )
        assert len(scheduled["buy"]) == 1
        assert scheduled["buy"][0].audience == "empty"
        assert scheduled["position_advice"] == []

    def test_empty_position_gets_normal_buy(self):
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals([self._entry()], [], holdings=[])
        assert len(scheduled["buy"]) == 1
        assert scheduled["buy"][0].audience == "empty"
        assert scheduled["position_advice"] == []


class TestForwardOnlyState:
    """同一事件不能被另一个模块拉回旧状态；filled 是唯一成交语义。"""

    def _event(self, status="valid"):
        from src.analyzers.signal_lifecycle import SignalEvent
        return SignalEvent(
            event_id="evt-20260907112434-002975-价量突破",
            stock_code="002975", stock_name="博杰股份",
            entry_type="价量突破", born_date="2026-09-07",
            expire_date="2026-09-12", breakout_level=99.39,
            entry_price=102.77, stop_loss=99.39,
            status=status,
        )

    def test_filled_cannot_roll_back_to_valid_or_triggered(self):
        from src.analyzers.signal_lifecycle import (
            InMemorySignalEventStore,
            can_transition_status,
            normalize_status,
        )
        store = InMemorySignalEventStore()
        event = self._event("filled")
        store.save(event)

        event.status = "valid"
        store.save(event)
        store.update_status(event.event_id, "triggered")

        assert store.events[event.event_id].status == "filled"
        assert can_transition_status("filled", "valid") is False
        # triggered 是旧库别名；作为目标值必须归一成 filled，不能新开状态。
        assert can_transition_status("filled", "triggered") is True
        assert normalize_status("triggered") == "filled"

    def test_price_below_buypoint_invalidates_without_close_or_low(self):
        from src.analyzers.signal_lifecycle import (
            InMemorySignalEventStore,
            SignalLifecycle,
        )
        lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
        event = lifecycle.register_event(
            "002975", "博杰股份", "价量突破",
            breakout_level=99.39, entry_price=102.77, stop_loss=99.39,
            target_low=112.89, target_high=120.21,
            hypothesis={"x": "放量突破MA25"},
        )
        notices = lifecycle.evaluate_events("002975", current_price=100.43)

        assert event.status == "invalidated"
        assert "回踩失败已撤单" in notices[0]["reason"]

    def test_price_far_above_buypoint_is_chase_abandoned(self):
        from src.analyzers.signal_lifecycle import (
            InMemorySignalEventStore,
            SignalLifecycle,
        )
        lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
        event = lifecycle.register_event(
            "688028", "沃尔德", "价量突破",
            breakout_level=88.0, entry_price=89.50, stop_loss=83.50,
            target_low=96.0, target_high=102.0,
            hypothesis={"x": "放量突破MA25"},
        )
        notices = lifecycle.evaluate_events("688028", current_price=104.0)

        assert event.status == "chase_abandon"
        assert notices[0]["exit_type"] == "追高放弃"
        assert "超过+15%" in notices[0]["reason"]

    def test_frozen_event_expires_after_validity(self):
        from datetime import timedelta as _timedelta
        from src.analyzers.signal_lifecycle import (
            InMemorySignalEventStore,
            SignalLifecycle,
        )
        lifecycle = SignalLifecycle(InMemorySignalEventStore(), valid_days=5)
        event = lifecycle.register_event(
            "688028", "沃尔德", "价量突破",
            breakout_level=88.0, entry_price=89.50, stop_loss=83.50,
            target_low=96.0, target_high=102.0,
            hypothesis={"x": "放量突破MA25"},
        )
        lifecycle.freeze(event.event_id, "技术-1（放量阴线），冻结")
        future = date.fromisoformat(event.born_date) + _timedelta(days=6)
        notices = lifecycle.evaluate_events(
            "688028", current_price=90.0, today=future,
        )

        assert event.status == "expired"
        assert notices[0]["exit_type"] == "信号过期"

    def test_active_filled_event_is_not_reentry_candidate(self):
        from src.analyzers.signal_lifecycle import reentry_status
        store = self._store_with_event("filled")
        status = reentry_status("002975", store=store)

        assert status["standby"] is False
        assert status["reason"] == "已成交事件在跟踪"
        assert status["event_id"].endswith("价量突破")

    def test_active_valid_event_is_not_reentry_candidate(self):
        from src.analyzers.signal_lifecycle import reentry_status
        store = self._store_with_event("valid")
        status = reentry_status("002975", store=store)

        assert status["standby"] is False
        assert status["reason"] == "已触发事件在跟踪"

    def _store_with_event(self, status):
        from src.analyzers.signal_lifecycle import InMemorySignalEventStore
        store = InMemorySignalEventStore()
        store.save(self._event(status))
        return store
