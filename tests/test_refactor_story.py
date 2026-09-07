"""
改造全链路闭环故事测试（集成）—— 按 9/3 → 9/4 沃尔德两天真实场景复盘

第1步【三】9/3 突破当日：假说完整 → 信号诞生（事件注册）→ 调度 → 落库（含 X/Y/Z/W）
第2步【三】9/4 状态重播被事件边界 + 生命周期去重拦下
第3步【六】回执 executed → 事件转 triggered，持仓进入配对出场跟踪
第4步【四】价格触及 W → 策略兑现（价位触发，非投票门）
第5步【六】卖出回执 → 盈亏/ZW 归类自动回填 → 分层统计可见
第6步【一】对照组：沃尔德式倒挂假说（买93.88/损93.94）在出厂即被拒
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


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "story.db")
    if hasattr(db._thread_local, "conn"):
        try:
            db._thread_local.conn.close()
        except Exception:
            pass
        db._thread_local.conn = None
    db.init_db()
    yield db
    if hasattr(db._thread_local, "conn"):
        try:
            db._thread_local.conn.close()
        except Exception:
            pass
        db._thread_local.conn = None


def _kline(bars=80, base=88.0, vol=1_000_000):
    kline = []
    for i in range(bars):
        close = base + (i % 7) * 0.4
        kline.append({
            "date": f"2026-06-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + 1.5, "low": close - 1.5,
            "close": close, "volume": vol, "amount": vol * close,
        })
    return kline


def _day1_tech():
    """9/3 突破当日：昨收 88.2 < 昨日MA25 88.5，今价 89.5 站上今日MA25 88.0，收阳"""
    return {
        "current_price": 89.5, "today_open": 88.8,
        "ma25": 88.0, "ma25_prev": 88.5, "prev_close": 88.2,
        "ma5": 88.8, "ma10": 88.6, "ma20": 87.5,
        "volume_ratio": 2.0, "turnover_rate": 4.0,
        "kline": _kline(),
        "today_volume": 2_000_000, "volume_ma60": 1_000_000,
        "recent_high": 92.0,
    }


def _day2_tech():
    """9/4：昨收 89.5 已在昨日MA25上方（状态延续，非事件）"""
    return {
        "current_price": 101.01, "today_open": 90.0,
        "ma25": 88.5, "ma25_prev": 88.0, "prev_close": 89.5,
        "ma5": 97.0, "ma10": 94.0, "ma20": 92.0,
        "volume_ratio": 2.0, "turnover_rate": 4.0,
        "kline": _kline(),
        "today_volume": 2_000_000, "volume_ma60": 1_000_000,
        "recent_high": 105.0,
    }


def test_full_closed_loop_story(tmp_db, monkeypatch):
    from src.analyzers.timing_engine import TimingEngine
    from src.decision.live_scheduler import schedule_live_signals
    from src.feedback.trade_logger import get_trade_logger
    from src.feedback.strategy_stats import evaluate_kill_switch, compute_strategy_stats

    te = TimingEngine(backtest_mode=False)
    tl = get_trade_logger()

    # ============ 第1步：9/3 信号诞生（假说完整 + 事件注册 + 调度 + 落库） ============
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": _day1_tech())
    signals = te.check_entry_signals("688028", "沃尔德", "defend", sector_status="main_trend")
    assert len(signals) == 1
    sig = signals[0]
    hyp = sig.hypothesis
    assert hyp["z"] < hyp["y"] < hyp["w"][0]                    # Z < Y < W 不变式
    assert sig.event_id

    entry_batch = [{
        "stock_code": sig.stock_code, "stock_name": sig.stock_name,
        "entry_type": sig.entry_type, "trigger_price": sig.entry_trigger_price,
        "confidence": sig.confidence, "stop_loss": sig.stop_loss,
        "hypothesis": hyp, "event_id": sig.event_id,
        "execution_plan": sig.execution_plan,
    }]
    scheduled = schedule_live_signals(entry_batch, [], holdings=[])
    assert len(scheduled["buy"]) == 1
    s = scheduled["buy"][0]
    assert s.audience == "empty"

    buy_id = tl.log_signal(
        signal_type="buy", stock_code="688028", stock_name="沃尔德",
        signal_data={
            "entry_type": s.entry_type, "trigger_price": s.trigger_price,
            "stop_loss": hyp["z"], "target_price": hyp["w"][0],
            "paired_z": hyp["z"], "paired_w_low": hyp["w"][0], "paired_w_high": hyp["w"][-1],
            "z_reference": hyp["z_reference"], "event_id": sig.event_id,
            "hypothesis": hyp,
        },
        shares=s.shares,
        note=s.schedule_note,
    )
    assert buy_id is not None

    # ============ 第2步：9/4 状态重播被拦（事件边界 + 生命周期去重） ============
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": _day2_tech())
    te.reset_caches()
    replay = te.check_entry_signals("688028", "沃尔德", "defend", sector_status="main_trend")
    assert replay == []          # 旧系统此处会再发一条（沃尔德连推两天的根源）

    # ============ 第3步：回执 executed → 事件转 triggered，持仓进入配对跟踪 ============
    assert tl.update_action(buy_id, "executed", actual_price=89.5) is True
    with tmp_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM signal_events WHERE event_id=?", (sig.event_id,))
        assert cursor.fetchone()["status"] == "triggered"
    position = tl.get_open_position("688028")
    assert position is not None and position["entry_type"] == "价量突破"

    # ============ 第4步：价格触及 W → 策略兑现（价位触发，非投票门） ============
    w_tech = {
        "current_price": 97.5, "ma5": 95.0, "ma10": 93.0, "ma20": 90.0,
        "volume_ratio": 1.5, "kline": _kline(base=95.0),
        "tech_signals": {},
    }
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": w_tech)
    te.reset_caches()
    exits = te.check_exit_signals("688028", "沃尔德", "defend")
    w_exits = [e for e in exits if e.exit_type == "策略兑现"]
    assert len(w_exits) == 1
    assert w_exits[0].source == "paired"
    assert "减半" in w_exits[0].reason or "清仓" in w_exits[0].reason

    # ============ 第5步：卖出回执 → 盈亏/ZW 自动回填 → 分层统计可见 ============
    sell_id = tl.log_signal(
        signal_type="sell", stock_code="688028", stock_name="沃尔德",
        signal_data={"exit_type": "策略兑现", "trigger_price": 97.5},
        shares=0,
    )
    assert tl.update_action(sell_id, "executed", actual_price=97.5) is True
    with tmp_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pnl_pct, zw_triggered, exit_price FROM trade_logs WHERE id=?", (buy_id,))
        closed = dict(cursor.fetchone())
    assert closed["zw_triggered"] == "W"
    assert closed["exit_price"] == 97.5
    assert closed["pnl_pct"] == pytest.approx(round((97.5 - 89.5) / 89.5 * 100, 2))

    stats = compute_strategy_stats()
    assert stats["价量突破"]["trades"] == 1
    assert stats["价量突破"]["wins"] == 1
    # 1 笔 < 50 → 不下线，只报告
    report = evaluate_kill_switch()
    assert report["价量突破"]["status"] == "active"

    # 归因回执（四行日志第四行）
    assert tl.set_review_outcome(buy_id, "logic_right", "突破有效，W位兑现") is True


def test_wald_inverted_hypothesis_rejected_at_factory(tmp_db):
    """对照组：沃尔德 9/4 式倒挂假说（买93.88/损93.94）出厂即拒，不进调度不推送"""
    from src.analyzers.signal_plan import build_execution_plan

    plan = build_execution_plan(
        entry_type="价量突破",
        benchmark_price=93.88, stop_loss=93.94, target_range=[113.0],
        tech_data={"kline": _kline(base=90.0)},
        hypothesis_x="放量突破MA25，回踩确认",
    )
    assert plan.execute is False
    assert any("止损倒挂" in r for r in plan.rejection_reasons)

    # 留痕写入 signal_rejections（可审计）
    from src.feedback.trade_logger import get_trade_logger
    get_trade_logger().log_rejection(
        stock_code="688028", stock_name="沃尔德", entry_type="价量突破",
        reasons=list(plan.rejection_reasons),
        detail={"benchmark_price": 93.88, "stop_loss": 93.94,
                "target_range": [113.0], "hypothesis": plan.hypothesis},
    )
    with tmp_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM signal_rejections")
        assert cursor.fetchone()["n"] == 1
