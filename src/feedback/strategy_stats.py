"""
策略分层统计与自动下线 — 让逻辑自己证明自己或杀死自己
====================================================

一套不能被自己的业绩杀死的逻辑，不算被厘清，只是被相信。

统计口径（回执闭环自动完成）：
  - 每笔交易四行日志：假说原文 / 实际出入场 / Z-W 是否触发 / 事后归因
  - 平仓回执时自动回填 pnl_pct 与 zw_triggered（trade_logger._link_exit_to_position）
  - 积累 min_trades_for_stats（默认 30）笔后按策略分层统计胜率和实际盈亏比

作废条件（自动下线，写入 strategy_status 表，调度器过滤 + 告警推送）：
  - 某策略滚动 kill_rolling_window（默认 50）笔期望值为负
  - 或胜率跌破盈亏平衡线对应水平：win_rate < 1 / (1 + payoff)
  - 样本不足 kill_min_trades（默认 50）笔时不下线，只报告
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from ..db import get_conn
from ..feedback.trade_logger import get_trade_logger

logger = logging.getLogger(__name__)


def _ensure_table(cursor) -> None:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS strategy_status (
        strategy TEXT PRIMARY KEY,
        status TEXT DEFAULT 'active',
        reason TEXT,
        stats_json TEXT,
        since TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


def compute_strategy_stats(closed_trades: Optional[List[Dict]] = None) -> Dict[str, Dict]:
    """按策略分层统计：笔数/胜率/均盈/均亏/盈亏比/期望/归因分布。"""
    trades = closed_trades if closed_trades is not None else get_trade_logger().get_closed_trades()
    stats: Dict[str, Dict] = {}
    for t in trades:
        strategy = str(t.get("strategy") or "未知策略")
        pnl = t.get("pnl_pct")
        if pnl is None:
            continue
        pnl = float(pnl)
        bucket = stats.setdefault(strategy, {
            "trades": 0, "wins": 0, "losses": 0,
            "sum_win": 0.0, "sum_loss": 0.0,
            "outcome": {"logic_right": 0, "luck": 0, "logic_wrong": 0, "unreviewed": 0},
        })
        bucket["trades"] += 1
        outcome = str(t.get("review_outcome") or "unreviewed")
        if outcome in bucket["outcome"]:
            bucket["outcome"][outcome] += 1
        else:
            bucket["outcome"]["unreviewed"] += 1
        if pnl > 0:
            bucket["wins"] += 1
            bucket["sum_win"] += pnl
        else:
            bucket["losses"] += 1
            bucket["sum_loss"] += pnl

    for strategy, bucket in stats.items():
        n = bucket["trades"]
        wins, losses = bucket["wins"], bucket["losses"]
        avg_win = bucket["sum_win"] / wins if wins else 0.0
        avg_loss = abs(bucket["sum_loss"] / losses) if losses else 0.0
        payoff = avg_win / avg_loss if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)
        win_rate = wins / n if n else 0.0
        expectancy = (bucket["sum_win"] + bucket["sum_loss"]) / n if n else 0.0
        breakeven = (1.0 / (1.0 + payoff)) if payoff > 0 else None
        bucket.update({
            "win_rate": round(win_rate, 4),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "payoff": round(payoff, 2) if payoff != float("inf") else None,
            "expectancy_pct": round(expectancy, 2),
            "breakeven_win_rate": round(breakeven, 4) if breakeven is not None else None,
        })
    return stats


def _kill_decision(
    bucket: Dict,
    rolling_window: int,
    kill_min_trades: int,
    closed_trades: List[Dict],
    strategy: str,
) -> Optional[str]:
    """下线判定：滚动窗口期望为负，或胜率跌破盈亏平衡线。样本不足返回 None。"""
    strategy_trades = [
        t for t in closed_trades
        if str(t.get("strategy") or "未知策略") == strategy
        and t.get("pnl_pct") is not None
    ]
    if len(strategy_trades) < kill_min_trades:
        return None
    rolling = strategy_trades[-rolling_window:]
    pnls = [float(t["pnl_pct"]) for t in rolling]
    expectancy = sum(pnls) / len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls)
    avg_win = sum(p for p in pnls if p > 0) / wins if wins else 0.0
    losses = len(pnls) - wins
    avg_loss = abs(sum(p for p in pnls if p <= 0) / losses) if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else 0.0
    breakeven = (1.0 / (1.0 + payoff)) if payoff > 0 else None

    if expectancy <= 0:
        be_text = ""
        if breakeven is not None:
            be_text = (f"且胜率{win_rate * 100:.1f}%跌破盈亏平衡线"
                       f"{breakeven * 100:.1f}%(盈亏比{payoff:.2f})")
        return (f"滚动{len(rolling)}笔期望值为负({expectancy:.2f}%){be_text}，策略下线重校")
    # 独立守卫：期望仍为正但胜率跌破盈亏平衡线（未来口径解耦时生效）
    if breakeven is not None and win_rate < breakeven:
        return (
            f"胜率{win_rate * 100:.1f}%跌破盈亏平衡线{breakeven * 100:.1f}%"
            f"(盈亏比{payoff:.2f})，策略下线重校"
        )
    return None


def get_strategy_status() -> Dict[str, Dict]:
    """读取策略状态表：{strategy: {status, reason, stats_json, since}}"""
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            _ensure_table(cursor)
            cursor.execute("SELECT * FROM strategy_status")
            rows = [dict(r) for r in cursor.fetchall()]
        return {r["strategy"]: r for r in rows}
    except Exception as e:
        logger.error("读取策略状态失败: %s", e)
        return {}


def get_offline_strategies() -> List[str]:
    """当前已下线策略列表（调度器过滤用）。"""
    return [
        strategy for strategy, row in get_strategy_status().items()
        if (row.get("status") or "active") == "offline"
    ]


def is_strategy_allowed(strategy: str) -> bool:
    return strategy not in get_offline_strategies()


def evaluate_kill_switch(
    config: Optional[Dict] = None,
    persist: bool = True,
) -> Dict[str, Dict]:
    """
    全量评估各策略的作废条件。

    返回 {strategy: {status, reason, stats, newly_offline}} —— newly_offline=True
    的策略由 engine 推送告警。写库（strategy_status）幂等。
    """
    config = config or {}
    rolling_window = int(config.get("kill_rolling_window", 50))
    kill_min_trades = int(config.get("kill_min_trades", 50))

    closed_trades = get_trade_logger().get_closed_trades()
    stats = compute_strategy_stats(closed_trades)
    existing = get_strategy_status()

    result: Dict[str, Dict] = {}
    for strategy, bucket in stats.items():
        reason = _kill_decision(bucket, rolling_window, kill_min_trades, closed_trades, strategy)
        if reason:
            status = "offline"
        else:
            status = "active"
            n = bucket["trades"]
            reason = (
                f"样本{n}笔" + (f"（不足{kill_min_trades}笔，暂不下线）" if n < kill_min_trades else "")
            )
        prev = existing.get(strategy)
        newly_offline = status == "offline" and (prev or {}).get("status") != "offline"
        result[strategy] = {
            "status": status,
            "reason": reason,
            "stats": bucket,
            "newly_offline": newly_offline,
        }
        if persist and (newly_offline or (prev or {}).get("status") != status or prev is None):
            try:
                with get_conn() as conn:
                    cursor = conn.cursor()
                    _ensure_table(cursor)
                    cursor.execute("""
                        INSERT OR REPLACE INTO strategy_status
                        (strategy, status, reason, stats_json, since, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        strategy, status, reason,
                        json.dumps(bucket, ensure_ascii=False, default=str),
                        (prev or {}).get("since") or datetime.now().isoformat(timespec="seconds"),
                        datetime.now().isoformat(timespec="seconds"),
                    ))
                    conn.commit()
            except Exception as e:
                logger.error("写入策略状态失败 %s: %s", strategy, e)
    return result


def format_strategy_report(offline: Optional[Dict[str, Dict]] = None) -> str:
    """分层统计报告（推送/CLI 用）：胜率/盈亏比/期望/归因分布/下线状态。"""
    if offline is None:
        offline = evaluate_kill_switch(persist=False)
    if not offline:
        return "暂无已平仓交易，记录闭环从第一笔回执开始积累。"
    lines = ["策略分层统计（回执闭环口径）:"]
    for strategy, info in offline.items():
        bucket = info.get("stats") or {}
        status = "✅在线" if info.get("status") == "active" else "⛔已下线"
        outcome = bucket.get("outcome") or {}
        reviewed = outcome.get("logic_right", 0) + outcome.get("luck", 0) + outcome.get("logic_wrong", 0)
        lines.append(
            f"  {strategy} [{status}] {bucket.get('trades', 0)}笔 | "
            f"胜率{(bucket.get('win_rate') or 0) * 100:.1f}% | "
            f"盈亏比{bucket.get('payoff') or 0:.2f} | "
            f"期望{(bucket.get('expectancy_pct') or 0):+.2f}% | "
            f"归因{reviewed}笔(对{outcome.get('logic_right', 0)}/运{outcome.get('luck', 0)}/错{outcome.get('logic_wrong', 0)})"
        )
        lines.append(f"    判定: {info.get('reason', '')}")
    return "\n".join(lines)
