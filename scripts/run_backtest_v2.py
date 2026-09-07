"""
优化版回测 — 解决性能问题
================================================
- 不每天 Slicing 全量 K 线（O(N²) → O(N)）
- 用游标推进：每只股票维护当前位置，避免重复扫描
- 跳过非交易日，按上证指数日期对齐
"""
import os
import sys
from pathlib import Path
import json
import logging
from datetime import datetime
from collections import defaultdict, Counter
from typing import Dict, List

import yaml

FIXED_ROOT = str(Path(__file__).parent.parent)  # 项目根目录
sys.path.insert(0, FIXED_ROOT)

# 性能优化：在导入 timing_engine 前，monkey-patch institutional_scorer
# 避免每次 _fetch_tech_data 都触发 30 秒的 akshare 调用
import src.analyzers.institutional_scorer as _inst
def _fast_score(stock_code):
    return {
        "vote_score": 0, "vote_label": "机构中性（回测跳过）",
        "votes": {}, "bullish_count": 0, "bearish_count": 0,
        "neutral_count": 4, "stale": False,
    }
_inst.score_institutional_holding = _fast_score

# 同时禁用 akshare 内部 tqdm
os.environ["TQDM_DISABLE"] = "1"

logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("backtest")

CACHE_PATH = str(Path(__file__).parent / "kline_cache.json")


def load_portfolio() -> List[Dict]:
    with open(Path(__file__).parent.parent / "config" / "portfolio.yaml", "r", encoding="utf-8") as f:
        p = yaml.safe_load(f)
    stocks = p.get("stocks") or []
    return [s for s in stocks if s.get("code") and len(s.get("code", "")) == 6]


def run_backtest_optimized(stocks: List[Dict], kline_data: Dict, index_kline: List[Dict]):
    """优化版回测 — 每只股票维护游标，避免重复 Slicing"""
    from src.analyzers.timing_engine import get_backtest_timing_engine

    te = get_backtest_timing_engine()

    # 上证指数的所有交易日
    all_dates = sorted([k["date"] for k in index_kline])
    print(f"回测区间: {all_dates[0]} ~ {all_dates[-1]}, 共 {len(all_dates)} 个交易日")

    # 为每只股票建立 date → 在 kline 中的索引 的映射
    stock_date_idx: Dict[str, Dict[str, int]] = {}
    for code, kline in kline_data.items():
        stock_date_idx[code] = {k["date"]: i for i, k in enumerate(kline)}

    # 上证指数 date → 在 index_kline 中的索引
    index_date_idx = {k["date"]: i for i, k in enumerate(index_kline)}

    # 限制每只股票的 K 线长度上限（性能优化）
    # timing_engine 计算最多需要 120 日 MA + 60 日均量 + 21 日量比 = 约 200 日
    MAX_KLINE_LEN = 250

    daily_signals = defaultdict(list)
    processed_days = 0

    for date_idx, date in enumerate(all_dates):
        if date_idx < 60:  # 跳过前 60 天
            continue

        # 切片所有股票的 K 线到 date（限制长度避免内存爆炸）
        sliced_kline: Dict[str, List[Dict]] = {}
        for code, kline in kline_data.items():
            # 找到 date 在该股票 K 线中的位置
            if date in stock_date_idx[code]:
                end_i = stock_date_idx[code][date] + 1
            else:
                # date 不是该股票的交易日，找最近的小于 date 的日期
                end_i = 0
                for d, i in stock_date_idx[code].items():
                    if d <= date:
                        end_i = max(end_i, i + 1)
            if end_i >= 60:
                # 只取最近 MAX_KLINE_LEN 条，避免内存爆炸
                start_i = max(0, end_i - MAX_KLINE_LEN)
                sliced_kline[code] = kline[start_i:end_i]

        if not sliced_kline:
            continue

        # 上证指数切片（也限制长度）
        if date in index_date_idx:
            idx_end = index_date_idx[date] + 1
            idx_start = max(0, idx_end - MAX_KLINE_LEN)
            sliced_index = index_kline[idx_start:idx_end]
        else:
            sliced_index = [k for k in index_kline if k["date"] <= date][-MAX_KLINE_LEN:]

        te.set_backtest_context(date, sliced_kline, sliced_index)
        te.prefetch_market_data()

        market_mode = "defend"
        sector_status = "rotational"

        for s in stocks:
            code = s["code"]
            name = s.get("name", code)
            if code not in sliced_kline:
                continue

            try:
                entry_sigs = te.check_entry_signals(
                    stock_code=code, stock_name=name,
                    market_mode=market_mode, sector_status=sector_status,
                    filter_result=None,
                )
                for sig in entry_sigs:
                    daily_signals[date].append({
                        "date": date, "code": code, "name": name,
                        "type": "buy",
                        "price": sig.entry_trigger_price,
                        "entry_type": sig.entry_type,
                        "exit_type": "",
                        "reason": (sig.trigger_reason or "")[:80],
                    })
            except Exception:  # 静默跳过（历史行为保持）
                pass

            try:
                exit_sigs = te.check_exit_signals(
                    stock_code=code, stock_name=name,
                    market_mode=market_mode, sector_status=sector_status,
                    sector_name="",
                )
                for sig in exit_sigs:
                    daily_signals[date].append({
                        "date": date, "code": code, "name": name,
                        "type": "sell",
                        "price": sig.trigger_price,
                        "entry_type": "",
                        "exit_type": sig.exit_type,
                        "reason": (sig.reason or "")[:80],
                    })
            except Exception:  # 静默跳过（历史行为保持）
                pass

        processed_days += 1
        if processed_days % 50 == 0:
            buy_cnt = sum(1 for s in daily_signals.get(date, []) if s["type"] == "buy")
            sell_cnt = sum(1 for s in daily_signals.get(date, []) if s["type"] == "sell")
            print(f"  [{processed_days}/{len(all_dates)-60}] {date}: 买{buy_cnt} 卖{sell_cnt}", flush=True)

    print(f"\n扫描完毕，共处理 {processed_days} 个交易日")

    # 买卖配对
    pairs = []
    by_stock = defaultdict(list)
    for date in sorted(daily_signals.keys()):
        for sig in daily_signals[date]:
            by_stock[sig["code"]].append(sig)

    for code, sigs in by_stock.items():
        i = 0
        while i < len(sigs) - 1:
            s = sigs[i]
            if s["type"] == "buy":
                j = i + 1
                while j < len(sigs) and sigs[j]["type"] != "sell":
                    j += 1
                if j < len(sigs):
                    sell = sigs[j]
                    buy_price = s["price"]
                    sell_price = sell["price"]
                    if buy_price > 0:
                        pnl_pct = (sell_price - buy_price) / buy_price * 100
                        try:
                            hold_days = (datetime.strptime(sell["date"], "%Y-%m-%d") -
                                         datetime.strptime(s["date"], "%Y-%m-%d")).days
                        except:
                            hold_days = 0
                        pairs.append({
                            "code": code, "name": s["name"],
                            "buy_date": s["date"], "sell_date": sell["date"],
                            "hold_days": hold_days,
                            "buy_price": buy_price, "sell_price": sell_price,
                            "pnl_pct": round(pnl_pct, 2),
                            "entry_type": s["entry_type"],
                            "exit_type": sell["exit_type"],
                        })
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1

    return pairs, daily_signals


def analyze(pairs: List[Dict], total_signals: int):
    print("\n" + "=" * 70)
    print("基于当前 timing_engine 代码的回测结果（2024-01 ~ 2026-07）")
    print("=" * 70)

    total = len(pairs)
    if total == 0:
        print("无配对数据")
        return None

    win = sum(1 for p in pairs if p["pnl_pct"] > 0.5)
    loss = sum(1 for p in pairs if p["pnl_pct"] < -0.5)
    flat = total - win - loss
    win_rate = win / total * 100

    win_pcts = [p["pnl_pct"] for p in pairs if p["pnl_pct"] > 0.5]
    loss_pcts = [p["pnl_pct"] for p in pairs if p["pnl_pct"] < -0.5]

    avg_win = sum(win_pcts) / len(win_pcts) if win_pcts else 0
    avg_loss = abs(sum(loss_pcts) / len(loss_pcts)) if loss_pcts else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")
    expected = (win / total) * avg_win - (loss / total) * avg_loss

    print(f"\n总信号数: {total_signals} (买+卖)")
    print(f"配对数: {total}")
    print(f"盈利: {win} | 亏损: {loss} | 持平: {flat}")
    print(f"胜率: {win_rate:.1f}%")
    print(f"平均盈利: +{avg_win:.2f}% (max=+{max(win_pcts) if win_pcts else 0:.2f}%, min=+{min(win_pcts) if win_pcts else 0:.2f}%)")
    print(f"平均亏损: -{avg_loss:.2f}% (max={max(loss_pcts) if loss_pcts else 0:.2f}%, min={min(loss_pcts) if loss_pcts else 0:.2f}%)")
    print(f"盈亏比: {profit_factor:.2f}")
    print(f"期望收益(每笔): {expected:+.2f}%")

    print("\n=== 按进场类型分组 ===")
    by_entry = defaultdict(list)
    for p in pairs:
        by_entry[p["entry_type"]].append(p)
    for et, ps in sorted(by_entry.items(), key=lambda x: -len(x[1])):
        w = sum(1 for p in ps if p["pnl_pct"] > 0.5)
        l = sum(1 for p in ps if p["pnl_pct"] < -0.5)
        t = len(ps)
        wr = w / t * 100 if t else 0
        avg_pnl = sum(p["pnl_pct"] for p in ps) / t if t else 0
        print(f"  {et or '(空)':12s}: {t:4d}笔 胜率{wr:5.1f}% 平均{avg_pnl:+6.2f}%")

    print("\n=== 按出场类型分组 ===")
    by_exit = defaultdict(list)
    for p in pairs:
        by_exit[p["exit_type"]].append(p)
    for et, ps in sorted(by_exit.items(), key=lambda x: -len(x[1])):
        w = sum(1 for p in ps if p["pnl_pct"] > 0.5)
        l = sum(1 for p in ps if p["pnl_pct"] < -0.5)
        t = len(ps)
        wr = w / t * 100 if t else 0
        avg_pnl = sum(p["pnl_pct"] for p in ps) / t if t else 0
        print(f"  {et or '(空)':12s}: {t:4d}笔 胜率{wr:5.1f}% 平均{avg_pnl:+6.2f}%")

    print("\n=== 持仓天数分布 ===")
    hold_buckets = Counter()
    for p in pairs:
        d = p["hold_days"]
        if d == 0: hold_buckets["当日"] += 1
        elif d <= 3: hold_buckets["1-3天"] += 1
        elif d <= 7: hold_buckets["4-7天"] += 1
        elif d <= 14: hold_buckets["8-14天"] += 1
        elif d <= 30: hold_buckets["15-30天"] += 1
        else: hold_buckets[">30天"] += 1
    for k in ["当日", "1-3天", "4-7天", "8-14天", "15-30天", ">30天"]:
        pct = hold_buckets[k] / total * 100 if total else 0
        print(f"  {k:8s}: {hold_buckets[k]:4d}笔 ({pct:5.1f}%)")

    print("\n=== 按年度分组 ===")
    by_year = defaultdict(list)
    for p in pairs:
        by_year[p["buy_date"][:4]].append(p)
    for yr, ps in sorted(by_year.items()):
        w = sum(1 for p in ps if p["pnl_pct"] > 0.5)
        l = sum(1 for p in ps if p["pnl_pct"] < -0.5)
        t = len(ps)
        wr = w / t * 100 if t else 0
        avg_pnl = sum(p["pnl_pct"] for p in ps) / t if t else 0
        print(f"  {yr}: {t:4d}笔 胜率{wr:5.1f}% 平均{avg_pnl:+6.2f}% (盈{w}/亏{l}/平{t-w-l})")

    print("\n=== 按股票分组（前 10）===")
    by_stock = defaultdict(list)
    for p in pairs:
        by_stock[f"{p['name']}({p['code']})"].append(p)
    for stk, ps in sorted(by_stock.items(), key=lambda x: -len(x[1]))[:10]:
        w = sum(1 for p in ps if p["pnl_pct"] > 0.5)
        t = len(ps)
        wr = w / t * 100 if t else 0
        avg_pnl = sum(p["pnl_pct"] for p in ps) / t if t else 0
        print(f"  {stk:25s}: {t:3d}笔 胜率{wr:5.1f}% 平均{avg_pnl:+6.2f}%")

    out_path = str(Path(__file__).parent / "pairs_detail.json")
    summary = {
        "total_signals": total_signals,
        "total_pairs": total,
        "win": win, "loss": loss, "flat": flat,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "expected_return": round(expected, 2),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "pairs": pairs}, f, ensure_ascii=False, indent=2)
    print(f"\n明细已保存: {out_path}")
    return summary


if __name__ == "__main__":
    print("=== 基于当前 timing_engine 代码的回测（优化版）===")
    print(f"策略代码: {FIXED_ROOT}/src/analyzers/timing_engine.py")
    print("评估方式: 买入信号 → 同股下一次卖出信号，按 trigger_price 涨跌幅")
    print("市场模式: defend (与实盘一致)")
    print()

    stocks = load_portfolio()
    print(f"portfolio.yaml: {len(stocks)} 只股票")

    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"K 线数据: {len(data['stocks'])} 只股票 + 上证指数 {len(data['index'])} 条")

    pairs, daily_signals = run_backtest_optimized(stocks, data["stocks"], data["index"])
    total_signals = sum(len(sigs) for sigs in daily_signals.values())
    analyze(pairs, total_signals)
