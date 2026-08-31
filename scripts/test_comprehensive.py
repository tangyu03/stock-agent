# -*- coding: utf-8 -*-
"""
真实数据全面检验：找出隐藏 bug + 不合理设计

逐项检查：
1. 止损价是否合理（是否高于当前价？是否为 0？）
2. 信号价格与实际成交价差异
3. 持有天数计算
4. 周线 MACD 在不同时间点的稳定性
5. 对子底检测的误判
6. 买入信号能否实际成交（涨跌停）
7. 回测净值曲线异常
8. 跳过信号的原因分布
"""
import os
import sys
import logging
from pathlib import Path
from collections import Counter

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")
logging.basicConfig(level=logging.WARNING)

from src.loop.data_loader import DataLoader
from src.loop.backtest_engine import BacktestEngine
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals, DEFAULT_BACKTEST_PARAMS
from src.analyzers.timing_engine import get_backtest_timing_engine

HOLDINGS = [
    ("688409", "富创精密"), ("688652", "京仪装备"), ("300843", "胜蓝股份"),
    ("688531", "日联科技"), ("688008", "澜起科技"), ("688041", "海光信息"),
    ("688027", "国盾量子"), ("301666", "大普微"), ("002594", "比亚迪"),
    ("920045", "蘅东光"), ("688820", "盛合晶微"), ("688028", "沃尔德"),
    ("300757", "罗博特科"), ("688521", "芯原股份"),
]

START_DATE = "2025-06-01"
END_DATE = "2026-07-10"


def main():
    print("=" * 70)
    print("  真实数据全面检验：找隐藏 bug + 不合理设计")
    print("=" * 70)

    loader = DataLoader()
    codes = [c for c, _ in HOLDINGS]
    kline_data = loader.load_kline(codes, START_DATE, END_DATE)
    if not kline_data:
        print("❌ 数据加载失败")
        return

    print(f"✅ 加载 {len(kline_data)} 只，共 {sum(len(v) for v in kline_data.values())} 根 K 线\n")

    # ============================================================
    # 检查 1: 止损价合理性
    # ============================================================
    print("━" * 70)
    print("  检查 1: 止损价合理性")
    print("━" * 70)

    engine = get_backtest_timing_engine()
    stop_loss_issues = []

    for code, name in HOLDINGS:
        if code not in kline_data:
            continue
        kline = kline_data[code]
        engine.set_backtest_context(kline[-1]["date"], {code: kline}, [])
        tech = engine._fetch_tech_data(code, "defend")
        stop_loss = engine.calculate_stop_loss(code, tech)
        current = tech.get("current_price", 0)

        # 检查：止损价是否高于当前价（不合理）
        if stop_loss.stop_loss_price > current:
            stop_loss_issues.append((code, name, "止损价高于当前价", current, stop_loss.stop_loss_price))
            print(f"  ❌ {code} {name}: 当前价 {current:.2f} < 止损价 {stop_loss.stop_loss_price:.2f}（止损价高于当前价！）")
        # 检查：止损价是否为 0
        elif stop_loss.stop_loss_price <= 0:
            stop_loss_issues.append((code, name, "止损价为 0", current, stop_loss.stop_loss_price))
            print(f"  ❌ {code} {name}: 止损价为 {stop_loss.stop_loss_price}")
        # 检查：止损价离当前价过远（>15%）
        elif current > 0 and (current - stop_loss.stop_loss_price) / current > 0.15:
            stop_loss_issues.append((code, name, "止损价过远", current, stop_loss.stop_loss_price))
            print(f"  ⚠️ {code} {name}: 当前价 {current:.2f}, 止损价 {stop_loss.stop_loss_price:.2f}（距离 {((current-stop_loss.stop_loss_price)/current*100):.1f}%）")
        else:
            print(f"  ✅ {code} {name}: 当前价 {current:.2f}, 止损价 {stop_loss.stop_loss_price:.2f}（距离 {((current-stop_loss.stop_loss_price)/current*100):.1f}%）")

    print(f"\n  止损价问题数: {len(stop_loss_issues)}")

    # ============================================================
    # 检查 2: 信号价格 vs 实际成交价差异
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 2: 信号价格 vs T+1 实际成交价差异")
    print("━" * 70)

    gen = StockAgentTunedV3Signals(market_mode="defend", params={**DEFAULT_BACKTEST_PARAMS})
    signals = gen.generate_signals(kline_data)

    # 构建 K 线索引
    kline_index = {}
    for code, rows in kline_data.items():
        kline_index[code] = {r["date"]: r for r in rows}

    all_dates = sorted(set(r["date"] for rows in kline_data.values() for r in rows))
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    price_diffs = []
    for sig in signals:
        # 找 T+1 日
        sig_idx = date_to_idx.get(sig.date, -1)
        if sig_idx < 0 or sig_idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[sig_idx + 1]
        next_kline = kline_index.get(sig.code, {}).get(next_day)
        if not next_kline:
            continue
        fill_open = float(next_kline.get("open", 0) or 0)
        if fill_open <= 0:
            continue
        diff_pct = (fill_open - sig.price) / sig.price * 100
        price_diffs.append((sig, next_day, fill_open, diff_pct))

    if price_diffs:
        diffs = [d[3] for d in price_diffs]
        print(f"  信号数: {len(price_diffs)}")
        print(f"  价差范围: {min(diffs):+.2f}% ~ {max(diffs):+.2f}%")
        print(f"  平均价差: {sum(diffs)/len(diffs):+.2f}%")
        print(f"  |价差|>3% 的信号数: {sum(1 for d in diffs if abs(d) > 3)}")

        # 显示价差最大的 5 个
        price_diffs.sort(key=lambda x: abs(x[3]), reverse=True)
        print("\n  价差最大的 5 个:")
        for sig, nd, fo, diff in price_diffs[:5]:
            print(f"    {sig.date} {sig.action} {sig.code} 信号价 {sig.price:.2f} → {nd} 成交价 {fo:.2f} ({diff:+.2f}%)")

    # ============================================================
    # 检查 3: 持有天数分布
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 3: 持有天数分布（卖出时）")
    print("━" * 70)

    # 配对买卖信号
    holdings_state = {}
    hold_days_list = []
    for sig in signals:
        if sig.action == "buy":
            if sig.code not in holdings_state:
                holdings_state[sig.code] = []
            holdings_state[sig.code].append({"date": sig.date, "shares": sig.shares, "price": sig.price})
        elif sig.action == "sell":
            if sig.code in holdings_state and holdings_state[sig.code]:
                buy = holdings_state[sig.code].pop(0)
                buy_idx = date_to_idx.get(buy["date"], -1)
                sell_idx = date_to_idx.get(sig.date, -1)
                if buy_idx >= 0 and sell_idx >= 0:
                    hold_days = sell_idx - buy_idx
                    hold_days_list.append(hold_days)

    if hold_days_list:
        print(f"  配对买卖数: {len(hold_days_list)}")
        print(f"  持有天数范围: {min(hold_days_list)} ~ {max(hold_days_list)} 天")
        print(f"  平均持有: {sum(hold_days_list)/len(hold_days_list):.1f} 天")
        # 分布
        buckets = {"0-3天": 0, "4-7天": 0, "8-15天": 0, "16-30天": 0, ">30天": 0}
        for d in hold_days_list:
            if d <= 3: buckets["0-3天"] += 1
            elif d <= 7: buckets["4-7天"] += 1
            elif d <= 15: buckets["8-15天"] += 1
            elif d <= 30: buckets["16-30天"] += 1
            else: buckets[">30天"] += 1
        print("  分布:")
        for k, v in buckets.items():
            print(f"    {k}: {v} 笔")

    # ============================================================
    # 检查 4: 跳过信号原因分布
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 4: 跳过信号原因分布")
    print("━" * 70)

    bt_engine = BacktestEngine(initial_cash=1_000_000)
    result = bt_engine.run(signals, kline_data)

    if result.skipped_signals:
        reasons = Counter(s["reason"] for s in result.skipped_signals)
        print(f"  跳过信号数: {len(result.skipped_signals)}")
        for reason, cnt in reasons.most_common():
            print(f"    {reason}: {cnt} 次")
    else:
        print("  无跳过信号")

    # ============================================================
    # 检查 5: 净值曲线异常点
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 5: 净值曲线异常点（日收益 >5% 或 <-5%）")
    print("━" * 70)

    daily_values = result.daily_values
    if len(daily_values) >= 2:
        abnormal_days = []
        for i in range(1, len(daily_values)):
            prev = daily_values[i-1]["value"]
            curr = daily_values[i]["value"]
            if prev > 0:
                ret = (curr - prev) / prev * 100
                if abs(ret) > 5:
                    abnormal_days.append((daily_values[i]["date"], ret, curr))

        if abnormal_days:
            print(f"  异常天数: {len(abnormal_days)}")
            for d, ret, val in abnormal_days[:10]:
                print(f"    {d}: {ret:+.2f}% (净值 {val:.0f})")
        else:
            print("  无异常波动日")

    # ============================================================
    # 检查 6: 同一只股票买卖信号冲突
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 6: 同日买卖信号冲突")
    print("━" * 70)

    sigs_by_date = {}
    for sig in signals:
        key = (sig.date, sig.code)
        if key not in sigs_by_date:
            sigs_by_date[key] = []
        sigs_by_date[key].append(sig)

    conflicts = []
    for (d, code), sigs in sigs_by_date.items():
        actions = set(s.action for s in sigs)
        if len(actions) > 1:
            conflicts.append((d, code, sigs))

    if conflicts:
        print(f"  冲突数: {len(conflicts)}")
        for d, code, sigs in conflicts[:5]:
            print(f"    {d} {code}: {[s.action for s in sigs]}")
    else:
        print("  无同日买卖冲突")

    # ============================================================
    # 检查 7: 买入信号时该股是否当日涨停（无法成交）
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 7: 买入信号当日是否涨停（无法成交）")
    print("━" * 70)

    from src.loop.backtest_engine import is_limit_up
    limit_up_buys = []
    for sig in signals:
        if sig.action != "buy":
            continue
        sig_idx = date_to_idx.get(sig.date, -1)
        if sig_idx < 0 or sig_idx + 1 >= len(all_dates):
            continue
        next_day = all_dates[sig_idx + 1]
        next_kline = kline_index.get(sig.code, {}).get(next_day)
        if not next_kline:
            continue
        fill_open = float(next_kline.get("open", 0) or 0)
        prev_close = float(next_kline.get("prev_close", 0) or fill_open)
        if is_limit_up(sig.code, prev_close, fill_open):
            limit_up_buys.append((sig, next_day, fill_open, prev_close))

    if limit_up_buys:
        print(f"  涨停无法买入数: {len(limit_up_buys)}")
        for sig, nd, fo, pc in limit_up_buys[:5]:
            chg = (fo - pc) / pc * 100 if pc > 0 else 0
            print(f"    {sig.date} 买 {sig.code} → {nd} 开盘 {fo:.2f} (涨幅 {chg:.1f}%)")
    else:
        print("  无涨停无法买入的情况")

    # ============================================================
    # 检查 8: 回测最终持仓（是否有未平仓）
    # ============================================================
    print(f"\n{'━'*70}")
    print("  检查 8: 回测结束时的持仓状态")
    print("━" * 70)

    if bt_engine.positions:
        print(f"  未平仓股票数: {len(bt_engine.positions)}")
        for code, pos in bt_engine.positions.items():
            if pos.shares > 0:
                name = next((n for c, n in HOLDINGS if c == code), code)
                print(f"    {code} {name}: {pos.shares} 股, 成本价 {pos.cost_price:.2f}, 买入日 {pos.buy_date}")
    else:
        print("  全部平仓")

    print(f"  最终现金: ¥{bt_engine.cash:,.2f}")
    print(f"  最终市值: ¥{result.final_value:,.2f}")

    # ============================================================
    # 汇总
    # ============================================================
    print(f"\n{'='*70}")
    print("  检验汇总")
    print(f"{'='*70}")
    print(f"  1. 止损价问题: {len(stop_loss_issues)} 个")
    print(f"  2. 信号价差>3%: {sum(1 for d in price_diffs if abs(d[3]) > 3) if price_diffs else 0} 个")
    print(f"  3. 持有天数: 平均 {sum(hold_days_list)/len(hold_days_list):.1f} 天" if hold_days_list else "  3. 无配对买卖")
    print(f"  4. 跳过信号: {len(result.skipped_signals)} 个")
    print(f"  5. 净值异常日: {len(abnormal_days) if 'abnormal_days' in dir() else 'N/A'} 个")
    print(f"  6. 同日买卖冲突: {len(conflicts)} 个")
    print(f"  7. 涨停无法买入: {len(limit_up_buys)} 个")
    print(f"  8. 未平仓股票: {sum(1 for p in bt_engine.positions.values() if p.shares > 0)} 只")


if __name__ == "__main__":
    main()
