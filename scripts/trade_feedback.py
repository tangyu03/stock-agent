"""
P1-3 交易回执 CLI — 推送后等待回执闭环

实盘推送落库（trade_logs user_action=pending）后，由用户对实际执行结果回执：

    python scripts/trade_feedback.py --list [--date 2026-08-15]
    python scripts/trade_feedback.py --execute <id> [--price 127.5] [--position 0.25]
    python scripts/trade_feedback.py --ignore <id>
    python scripts/trade_feedback.py --modified <id> --price 127.5 [--position 0.25]
    python scripts/trade_feedback.py --holdings        # 查看当前聚合持仓

回执后 user_action 变 executed/ignored/modified：
  - executed 记录参与 get_current_holdings 聚合（buy/t0_buy 加仓，sell/t0_sell 减仓）
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
        note = (r["note"] or "").replace("\n", " ")[:60]
        if note:
            print(f"        {note}")


def cmd_execute(tl, log_id, price, position):
    if tl.update_action(log_id, "executed", price or 0, position or 0):
        print(f"#{log_id} → 已执行" + (f"（成交价 {_fmt_price(price)}）" if price else ""))
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


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="P1-3 交易回执 CLI（推送后等待回执）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出待回执信号")
    g.add_argument("--execute", type=int, metavar="ID", help="标记为已执行")
    g.add_argument("--ignore", type=int, metavar="ID", help="标记为已忽略")
    g.add_argument("--modified", type=int, metavar="ID", help="标记为已修改")
    g.add_argument("--holdings", action="store_true", help="查看当前聚合持仓")
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
