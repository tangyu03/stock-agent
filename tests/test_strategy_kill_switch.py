"""
【六】记录闭环与策略自动下线 — 回归测试

每笔交易四行日志：假说原文 / 实际出入场 / Z-W 是否触发 / 事后归因。
分层统计（30笔起）→ 作废条件（滚动50笔期望为负 / 胜率跌破盈亏平衡线）→
策略下线重校（调度器自动屏蔽 + 告警）。
一套不能被自己的业绩杀死的逻辑，不算被厘清，只是被相信。
"""
import sys
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """隔离的 SQLite（含全部新表），每个用例独立"""
    import src.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_agent.db")
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


def _closed_trade(strategy, pnl, reviewed="unreviewed", day=1):
    return {
        "stock_code": "688028",
        "strategy": strategy,
        "pnl_pct": pnl,
        "zw_triggered": "W" if pnl > 0 else "Z",
        "review_outcome": reviewed,
        "entry_date": f"2026-08-{day:02d}",
        "exit_date": f"2026-08-{day + 3:02d}",
    }


class TestStrategyStats:
    def test_stats_grouped_by_strategy(self):
        from src.feedback.strategy_stats import compute_strategy_stats
        trades = [
            _closed_trade("价量突破", 5.0, "logic_right"),
            _closed_trade("价量突破", 3.0, "logic_right"),
            _closed_trade("价量突破", -2.0, "logic_wrong"),
            _closed_trade("恐慌抄底", -4.0, "logic_wrong"),
        ]
        stats = compute_strategy_stats(trades)
        bk = stats["价量突破"]
        assert bk["trades"] == 3
        assert bk["wins"] == 2 and bk["losses"] == 1
        assert bk["win_rate"] == round(2 / 3, 4)
        assert bk["avg_win_pct"] == 4.0
        assert bk["avg_loss_pct"] == 2.0
        assert bk["payoff"] == 2.0
        assert bk["expectancy_pct"] == round((5.0 + 3.0 - 2.0) / 3, 2)
        assert bk["breakeven_win_rate"] == round(1 / 3, 4)
        assert bk["outcome"]["logic_right"] == 2
        assert bk["outcome"]["logic_wrong"] == 1

    def test_kill_switch_offline_on_negative_expectancy(self):
        """滚动 50 笔期望值为负 → 策略下线（自动，非人工确认）"""
        from src.feedback.strategy_stats import evaluate_kill_switch
        trades = [
            _closed_trade("套利低吸", -1.0 if i % 2 == 0 else 0.5, day=(i % 27) + 1)
            for i in range(50)
        ]
        # 期望 = (25×-1 + 25×0.5)/50 = -0.25% < 0
        report = evaluate_kill_switch(persist=False)
        # 直接注入统计的决策验证：用内部函数走全流程
        from src.feedback.strategy_stats import _kill_decision, compute_strategy_stats
        stats = compute_strategy_stats(trades)
        reason = _kill_decision(stats["套利低吸"], 50, 50, trades, "套利低吸")
        assert reason is not None
        assert "期望值为负" in reason

    def test_kill_switch_respects_min_trades(self):
        """样本不足 50 笔 → 不下线（只报告）"""
        from src.feedback.strategy_stats import _kill_decision, compute_strategy_stats
        trades = [_closed_trade("套利低吸", -1.0, day=(i % 27) + 1) for i in range(30)]
        stats = compute_strategy_stats(trades)
        reason = _kill_decision(stats["套利低吸"], 50, 50, trades, "套利低吸")
        assert reason is None

    def test_kill_switch_on_winrate_below_breakeven(self):
        """胜率跌破盈亏平衡线（与期望为负同条件，合并告警文案）→ 下线"""
        from src.feedback.strategy_stats import _kill_decision, compute_strategy_stats
        # 50 笔：24 赚 2%、26 亏 2% → payoff=1.0，平衡线 50%，胜率 48% < 50%，
        # 期望 = -0.08% < 0 → 两种表述同时出现在下线理由里
        trades = []
        for i in range(50):
            trades.append(_closed_trade("确认追强", 2.0 if i < 24 else -2.0, day=(i % 27) + 1))
        stats = compute_strategy_stats(trades)
        reason = _kill_decision(stats["确认追强"], 50, 50, trades, "确认追强")
        assert reason is not None
        assert "盈亏平衡线" in reason
        assert "期望值为负" in reason

    def test_persist_and_offline_filter(self, tmp_db, monkeypatch):
        """下线判定写库；调度器自动屏蔽该策略的新买入"""
        from src.feedback import strategy_stats as ss
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()

        # 造 50 笔已平仓亏损交易（回执闭环口径）
        for i in range(50):
            tl.log_signal(
                signal_type="buy", stock_code="688028", stock_name="沃尔德",
                signal_data={
                    "entry_type": "套利低吸", "trigger_price": 100.0,
                    "stop_loss": 95.0, "target_price": 103.0,
                    "hypothesis": {"x": "周线趋势低吸", "y": 100, "z": 95, "w": [103],
                                   "sentence": "因为周线趋势低吸…"},
                },
                shares=100, user_action="executed", actual_price=100.0,
            )
        # 直接构造已平仓口径：手工把 50 笔写成 exit_price>0 + 负 pnl
        with tmp_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE trade_logs SET exit_price=95.0, exit_date='2026-08-20', pnl_pct=-5.0 "
                "WHERE stock_code='688028' AND signal_type='buy'"
            )
            conn.commit()

        report = ss.evaluate_kill_switch()
        assert report.get("套利低吸", {}).get("status") == "offline"
        assert report["套利低吸"]["newly_offline"] is True

        # 状态表落库 + 查询
        offline = ss.get_offline_strategies()
        assert offline == ["套利低吸"]

        # 调度器过滤
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            [{
                "stock_code": "688129", "stock_name": "测试",
                "entry_type": "套利低吸", "trigger_price": 10.0,
                "confidence": "高",
                "execution_plan": {"execute": True, "benchmark_price": 10.0,
                                   "execution_tiers": [{"role": "main", "price": 10.0}]},
            }],
            [],
            offline_strategies=offline,
        )
        assert scheduled["buy"] == []
        assert scheduled["stats"]["buy_strategy_offline"] == 1
        assert scheduled["skipped"]["buy_strategy_offline"][0]["entry_type"] == "套利低吸"

    def test_format_strategy_report(self):
        from src.feedback.strategy_stats import format_strategy_report
        trades = [
            _closed_trade("价量突破", 5.0, "logic_right"),
            _closed_trade("价量突破", -2.0, "logic_wrong"),
        ]
        from src.feedback.strategy_stats import evaluate_kill_switch
        report = evaluate_kill_switch.__wrapped__ if hasattr(evaluate_kill_switch, "__wrapped__") else None
        text = format_strategy_report({
            "价量突破": {
                "status": "active",
                "reason": "样本2笔（不足50笔，暂不下线）",
                "stats": compute_stats_lite(trades),
                "newly_offline": False,
            }
        })
        assert "价量突破" in text
        assert "胜率" in text and "盈亏比" in text and "期望" in text


def compute_stats_lite(trades):
    from src.feedback.strategy_stats import compute_strategy_stats
    return compute_strategy_stats(trades)["价量突破"]


class TestTradeLogHypothesis:
    """【六】四行日志落库：假说原文 + 配对 Z/W（推送时写入）"""

    def test_buy_log_records_full_hypothesis(self, tmp_db):
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()
        log_id = tl.log_signal(
            signal_type="buy", stock_code="688028", stock_name="沃尔德",
            signal_data={
                "entry_type": "价量突破", "trigger_price": 89.5,
                "stop_loss": 83.5, "target_price": 96.0,
                "paired_z": 83.5, "paired_w_low": 96.0, "paired_w_high": 102.0,
                "event_id": "evt-test-1",
                "hypothesis": {
                    "x": "放量突破MA25", "y": 89.5, "z": 83.5, "w": [96.0, 102.0],
                    "sentence": "因为放量突破MA25，所以在89.50买入…",
                },
            },
            shares=100, note="调度备注 | 假说: 因为放量突破MA25…",
        )
        assert log_id is not None
        with tmp_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_logs WHERE id=?", (log_id,))
            row = dict(cursor.fetchone())
        assert row["hypothesis_x"] == "放量突破MA25"
        assert row["paired_z"] == 83.5
        assert row["paired_w_low"] == 96.0
        assert row["paired_w_high"] == 102.0
        assert row["event_id"] == "evt-test-1"
        assert row["stop_loss"] == 83.5          # 修复：原实现从未写入
        assert row["target_price"] == 96.0

    def test_sell_receipt_links_exit_to_position(self, tmp_db):
        """卖出回执（update_action）→ 自动回填开仓行 exit_price/pnl_pct/zw_triggered"""
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()
        buy_id = tl.log_signal(
            signal_type="buy", stock_code="688028", stock_name="沃尔德",
            signal_data={
                "entry_type": "价量突破", "trigger_price": 89.5,
                "stop_loss": 83.5, "target_price": 96.0,
                "paired_z": 83.5, "paired_w_low": 96.0, "paired_w_high": 102.0,
                "hypothesis": {"x": "放量突破", "y": 89.5, "z": 83.5, "w": [96.0, 102.0]},
            },
            shares=100, user_action="executed", actual_price=89.5,
        )
        # 卖出先落库 pending，再回执 executed → 触发回填联动
        sell_id = tl.log_signal(
            signal_type="sell", stock_code="688028", stock_name="沃尔德",
            signal_data={"exit_type": "策略兑现", "trigger_price": 98.0},
            shares=0,
        )
        assert tl.update_action(sell_id, "executed", actual_price=98.0) is True
        with tmp_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_logs WHERE id=?", (buy_id,))
            row = dict(cursor.fetchone())
        assert row["exit_price"] == 98.0
        assert row["zw_triggered"] == "W"                    # 策略兑现 → W
        assert abs(row["pnl_pct"] - round((98.0 - 89.5) / 89.5 * 100, 2)) < 0.01
        assert row["exit_date"]

    def test_review_outcome_receipt(self, tmp_db):
        """事后归因回执：logic_right / luck / logic_wrong"""
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()
        log_id = tl.log_signal(
            signal_type="buy", stock_code="688028", stock_name="沃尔德",
            signal_data={"entry_type": "价量突破", "trigger_price": 89.5},
            shares=100,
        )
        assert tl.set_review_outcome(log_id, "logic_right", "突破有效，回踩不破") is True
        assert tl.set_review_outcome(log_id, "invalid_choice") is False
        with tmp_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT review_outcome, review_note FROM trade_logs WHERE id=?", (log_id,))
            row = cursor.fetchone()
        assert row["review_outcome"] == "logic_right"
        assert "突破有效" in row["review_note"]

    def test_open_position_provides_paired_hypothesis(self, tmp_db):
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()
        tl.log_signal(
            signal_type="buy", stock_code="688028", stock_name="沃尔德",
            signal_data={
                "entry_type": "价量突破", "trigger_price": 89.5,
                "stop_loss": 83.5, "target_price": 96.0,
                "paired_z": 83.5, "paired_w_low": 96.0, "paired_w_high": 102.0,
                "z_reference": 88.0,
                "hypothesis": {"x": "放量突破", "y": 89.5, "z": 83.5, "w": [96.0, 102.0]},
            },
            shares=100, user_action="executed", actual_price=89.5,
        )
        position = tl.get_open_position("688028")
        assert position is not None
        assert position["entry_type"] == "价量突破"
        assert position["paired_z"] == 83.5
        assert position["paired_w_low"] == 96.0
        assert position["z_reference"] == 88.0
        # 无持仓
        assert tl.get_open_position("688999") is None

    def test_pending_buy_signal_is_usable_for_paired_exit(self, monkeypatch):
        """未回执的信号也是可证伪假说：配对出场无需用户手动 --execute。"""
        import src.db as db
        from src.feedback.trade_logger import get_trade_logger

        from pathlib import Path

        monkeypatch.setattr(db, "DB_PATH", Path(":memory:"))
        if hasattr(db._thread_local, "conn"):
            try:
                db._thread_local.conn.close()
            except Exception:
                pass
            db._thread_local.conn = None
        db.init_db()

        tl = get_trade_logger()
        tl.log_signal(
            signal_type="buy", stock_code="002975", stock_name="博杰股份",
            signal_data={
                "entry_type": "价量突破", "trigger_price": 102.28,
                "stop_loss": 98.48, "target_price": 110.46,
                "paired_z": 98.48, "paired_w_low": 110.46, "paired_w_high": 117.62,
                "hypothesis": {"x": "放量突破MA25", "y": 102.28, "z": 98.48,
                               "w": [110.46, 117.62]},
            },
            shares=2400,
        )
        paired = tl.get_paired_position("002975")
        assert paired is not None
        assert paired["user_action"] == "pending"
        assert paired["paired_z"] == 98.48
        # pending 只用于配对出场，不进入真实持仓聚合
        assert tl.get_open_position("002975") is None
        assert tl.get_current_holdings() == []


class TestRejectionLogging:
    """【一】出厂拒绝留痕表"""

    def test_rejection_is_logged_for_audit(self, tmp_db):
        from src.feedback.trade_logger import get_trade_logger
        tl = get_trade_logger()
        tl.log_rejection(
            stock_code="688028", stock_name="沃尔德", entry_type="价量突破",
            reasons=["止损倒挂(Z>=Y): 认错价93.94高于买点93.88，假说自相矛盾"],
            detail={
                "benchmark_price": 93.88, "stop_loss": 93.94,
                "target_range": [113.0],
                "hypothesis": {"x": "放量突破MA25", "y": 93.88, "z": 93.94, "w": [113.0]},
            },
        )
        with tmp_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signal_rejections ORDER BY id DESC")
            row = dict(cursor.fetchone())
        assert row["stock_code"] == "688028"
        assert row["entry_type"] == "价量突破"
        assert "止损倒挂" in row["reason"]
        assert row["missing_fields"] == ""       # 四要素都在，败在不变式
