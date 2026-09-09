"""
P1-3 + 【六】交易回执 CLI — 推送后等待回执闭环

实盘推送落库（trade_logs user_action=pending）后，由用户对实际执行结果回执：

    python scripts/trade_feedback.py --list [--date 2026-08-15]
    python scripts/trade_feedback.py --execute <id> [--price 127.5] [--position 0.25]
    python scripts/trade_feedback.py --ignore <id>
    python scripts/trade_feedback.py --modified <id> --price 127.5 [--position 0.25]
    python scripts/trade_feedback.py --holdings        # 查看当前聚合持仓

【六】记录闭环扩展（四行日志）：
    # 事后归因（逻辑对了/运气/逻辑错了）
    python scripts/trade_feedback.py --outcome <id> logic_right [--note "突破有效回踩不破"]
    python scripts/trade_feedback.py --outcome <id> luck      [--note "反弹碰巧"]
    python scripts/trade_feedback.py --outcome <id> logic_wrong [--note "量能口径坏了"]

    # 分层统计（按策略：胜率/盈亏比/期望/归因分布，30 笔起）
    python scripts/trade_feedback.py --stats

    # 策略在线状态（含下线原因）
    python scripts/trade_feedback.py --strategies

回执后 user_action 变 executed/ignored/modified：
  - executed 记录参与 get_current_holdings 聚合（buy/t0_buy 加仓，sell/t0_sell 减仓）
  - buy executed 且带 event_id → 信号事件转 triggered（受众分流/生命周期）
  - sell executed → 自动回填开仓行 exit_price/pnl_pct/zw_triggered（Z/W 归类）
  - 持仓来源从此闭环，替代 add_plans 占位口径
"""
import argparse
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.feedback.trade_logger import get_trade_logger  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _fmt_price(v):
    return f"{v:.2f}" if v else "-"


def cmd_list(tl, date_str):
    rows = tl.get_pending_signals(date_str)
    if not rows:
        print(f"[{date_str or '全部'}] 无待回执信号")
        return
    print(f"待回执信号（{date_str or '全部'}，{len(rows)} 条）：")
    for r in rows:
        et = r["entry_type"] or r["exit_type"] or ""
        print(f"  #{r['id']:<4} {r['date']} {r['time']} {r['signal_type']:<5} "
              f"{r['stock_code']} {r['stock_name']} {et} @ {_fmt_price(r['trigger_price'])} "
              f"股数={int(r['shares'] or 0)}")
        hyp = r.get("hypothesis_sentence") or ""
        if hyp:
            print(f"        假说: {hyp[:90]}")
        note = (r["note"] or "").replace("\n", " ")[:60]
        if note:
            print(f"        {note}")


def cmd_execute(tl, log_id, price, position):
    if tl.update_action(log_id, "executed", price or 0, position or 0):
        print(f"#{log_id} → 已执行" + (f"（成交价 {_fmt_price(price)}）" if price else ""))
        print("  回执联动：买入→事件转triggered；卖出→自动回填盈亏与Z/W归类")
    else:
        print(f"#{log_id} 更新失败（id 不存在？）")


def cmd_ignore(tl, log_id):
    if tl.update_action(log_id, "ignored"):
        print(f"#{log_id} → 已忽略")
    else:
        print(f"#{log_id} 更新失败")


def cmd_modified(tl, log_id, price, position):
    if tl.update_action(log_id, "modified", price or 0, position or 0):
        print(f"#{log_id} → 已修改" + (f"（成交价 {_fmt_price(price)}）" if price else ""))
    else:
        print(f"#{log_id} 更新失败")


def cmd_holdings(tl):
    holdings = tl.get_current_holdings()
    if not holdings:
        print("当前持仓：空")
        return
    total = sum(h["shares"] * h["cost_price"] for h in holdings)
    print("当前持仓（trade_logs executed 聚合）：")
    for h in holdings:
        print(f"  {h['code']} {h['stock_name']} {h['shares']}股 @ {_fmt_price(h['cost_price'])}")
    print(f"  合计成本 ≈ {total:,.0f} 元")


def cmd_outcome(tl, log_id, outcome, note):
    """【六】事后归因回执：logic_right（逻辑对了）/ luck（运气）/ logic_wrong（逻辑错了）"""
    if tl.set_review_outcome(log_id, outcome, note or ""):
        labels = {"logic_right": "逻辑对了", "luck": "运气", "logic_wrong": "逻辑错了"}
        print(f"#{log_id} → 归因：{labels[outcome]}" + (f"（{note}）" if note else ""))
    else:
        print(f"#{log_id} 归因失败（id 不存在或取值非法？）")


def cmd_stats():
    """【六】分层统计：按策略的胜率/盈亏比/期望/归因分布"""
    from src.feedback.strategy_stats import format_strategy_report
    print(format_strategy_report())


def cmd_strategies():
    """【六】策略在线状态（含下线原因）"""
    from src.feedback.strategy_stats import get_strategy_status, evaluate_kill_switch
    evaluate_kill_switch()
    status = get_strategy_status()
    if not status:
        print("暂无策略状态记录（无已平仓交易时不评估）")
        return
    print("策略状态：")
    for strategy, row in status.items():
        flag = "✅在线" if (row.get("status") or "active") == "active" else "⛔已下线"
        print(f"  {strategy} [{flag}] {row.get('reason') or ''}")


def cmd_zmode():
    """【P0-2】Z 线双模式对照（作废条件判定材料：30笔起步）"""
    from src.feedback.strategy_stats import z_mode_comparison
    comparison = z_mode_comparison()
    if not comparison:
        print("暂无携带 z_line_mode 的闭合样本")
        return
    print("Z线模式对照（裸结构位 vs ATR缓冲——作废条件：缓冲版期望显著更高时切换）：")
    for mode, info in comparison.items():
        print(f"  {mode}: {info.get('note', '')}")


def cmd_sensitivity():
    """【P0-3】分级 OR 过敏感统计（周触发>5次 → 回调结构位距离参数而非回退AND）"""
    from src.feedback.strategy_stats import graded_exit_sensitivity
    report = graded_exit_sensitivity()
    print(f"近7日止损类触发: {report.get('week_triggers', 0)}次")
    if report.get("note"):
        print(f"  判定: {report['note']}")
    for row in report.get("recent") or []:
        print(f"  {row.get('exit_date')} {row.get('stock_name', row.get('stock_code'))} "
              f"{row.get('exit_type')} @{row.get('exit_price')}")


def cmd_sector_versions():
    """【P2-1】板块版本化统计（历史统计按分类口径版本切分）"""
    from src.feedback.strategy_stats import sector_version_breakdown
    counts = sector_version_breakdown()
    print("闭合样本按板块分类版本分布（9/4→9/7 分类换血，新旧口径不互污）：")
    for version, count in sorted(counts.items()):
        print(f"  {version}: {count}笔")


def cmd_projection_errors():
    """【P0-1】外推误差记录表（作废条件：10:30后误差持续>15% → 换标的池分位表）"""
    from src.analyzers.volume_projection import projection_error_report
    report = projection_error_report()
    print("量能外推误差记录（收盘 vs 盘中外推，误差进记录表）：")
    print(f"  样本: {report.get('samples', 0)}个（10:30后口径）")
    if report.get("mean_error_pct") is not None:
        print(f"  平均误差: {report['mean_error_pct']}% | 最大: {report['max_error_pct']}%")
    print(f"  判定: {report.get('note', '')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="P1-3 + 记录闭环 交易回执 CLI")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出待回执信号")
    g.add_argument("--execute", type=int, metavar="ID", help="标记为已执行")
    g.add_argument("--ignore", type=int, metavar="ID", help="标记为已忽略")
    g.add_argument("--modified", type=int, metavar="ID", help="标记为已修改")
    g.add_argument("--holdings", action="store_true", help="查看当前聚合持仓")
    g.add_argument("--outcome", type=int, metavar="ID",
                   help="事后归因（logic_right/luck/logic_wrong）")
    g.add_argument("--stats", action="store_true", help="策略分层统计（胜率/盈亏比/期望）")
    g.add_argument("--strategies", action="store_true", help="策略在线状态")
    # 【Phase4】决策记录作废条件的审计入口（每周复盘逐条对照）
    g.add_argument("--zmode", action="store_true", help="Z线双模式对照（P0-2 作废条件）")
    g.add_argument("--sensitivity", action="store_true", help="分级OR过敏感统计（P0-3 作废条件）")
    g.add_argument("--sector-versions", action="store_true", help="板块版本分布（P2-1）")
    g.add_argument("--projection-errors", action="store_true", help="外推误差记录表（P0-1 作废条件）")
    p.add_argument("--outcome-value", choices=["logic_right", "luck", "logic_wrong"],
                   default=None, help="归因取值（--outcome 用）")
    p.add_argument("--note", default="", help="归因备注（--outcome 用）")
    p.add_argument("--date", default=None, help="日期 YYYY-MM-DD（--list 用）")
    p.add_argument("--price", type=float, default=0, help="实际成交价")
    p.add_argument("--position", type=float, default=0, help="实际仓位（0-1）")
    a = p.parse_args()

    tl = get_trade_logger()
    if a.list:
        cmd_list(tl, a.date)
    elif a.execute:
        cmd_execute(tl, a.execute, a.price, a.position)
    elif a.ignore:
        cmd_ignore(tl, a.ignore)
    elif a.modified:
        cmd_modified(tl, a.modified, a.price, a.position)
    elif a.holdings:
        cmd_holdings(tl)
    elif a.outcome:
        if not a.outcome_value:
            p.error("--outcome 需要配合 --outcome-value logic_right|luck|logic_wrong")
        cmd_outcome(tl, a.outcome, a.outcome_value, a.note)
    elif a.stats:
        cmd_stats()
    elif a.strategies:
        cmd_strategies()
    elif a.zmode:
        cmd_zmode()
    elif a.sensitivity:
        cmd_sensitivity()
    elif a.sector_versions:
        cmd_sector_versions()
    elif a.projection_errors:
        cmd_projection_errors()
