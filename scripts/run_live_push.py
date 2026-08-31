"""v3 自适应策略 + PushPlus 实盘推送"""
import os, sys, logging, argparse
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
if sys.platform != "win32":
    os.environ.setdefault("TZ", "Asia/Shanghai")  # Windows 不设（CRT 不识 Area/City 格式会回退 UTC）
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
logging.getLogger("src.data_layer.akshare_adapter").setLevel(logging.WARNING)
logging.getLogger("src.data_layer.skill_wrapper").setLevel(logging.WARNING)

from src.loop.data_loader import DataLoader
from src.loop.stockagent_tuned_v3_signals import StockAgentTunedV3Signals
from src.push.pushplus import get_pushplus

DEFAULT_STOCKS = [
    ("688256","寒武纪"),("688041","海光信息"),("301005","超捷股份"),("301232","飞沃科技"),
    ("300308","中际旭创"),("300502","新易盛"),("688820","盛合晶微"),("688321","微芯生物"),
    ("688037","芯源微"),("688110","东芯股份"),("688521","芯原股份"),("600367","红星发展"),
]
LOOKBACK_DAYS = 60

def main():
    parser = argparse.ArgumentParser(description="v3 自适应策略 + PushPlus 推送")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描不推送")
    parser.add_argument("--stocks", type=str, default="", help="自定义股票代码")
    args = parser.parse_args()

    print(f"\n{'='*60}\n  v3 自适应策略 + PushPlus 实盘推送\n  当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}")

    stocks = DEFAULT_STOCKS
    if args.stocks:
        codes = args.stocks.split(",")
        stocks = [(c.strip(), c.strip()) for c in codes]

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    print("\n📥 加载最近行情数据...")
    loader = DataLoader()
    codes = [s[0] for s in stocks]
    kline_data = loader.load_kline(codes, start_date, end_date)
    if not kline_data:
        print("❌ 数据加载失败"); return
    total = sum(len(v) for v in kline_data.values())
    print(f"   ✅ {len(kline_data)} 只，共 {total} 根K线")

    import akshare as ak
    index_kline = []
    try:
        df = ak.stock_zh_index_daily(symbol="sh000001")
        for _, r in df.tail(30).iterrows():
            ds = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
            if start_date <= ds <= end_date:
                index_kline.append({"date": ds, "open": float(r["open"]), "close": float(r["close"]),
                    "high": float(r["high"]), "low": float(r["low"]), "volume": float(r["volume"])})
        print(f"   ✅ 上证指数：{len(index_kline)} 根")
    except Exception as e:
        print(f"   ⚠️ 上证指数失败：{e}")

    print("\n📊 跑 v3 自适应策略...")
    gen = StockAgentTunedV3Signals(market_mode="defend", adaptive_mode=True,
        index_kline=index_kline, params={"backtest_mode": True})
    signals = gen.generate_signals(kline_data)
    all_dates = sorted(set(s.date for s in signals))
    today_signals = []
    if all_dates:
        latest_dates = set(all_dates[-2:])
        today_signals = [s for s in signals if s.date in latest_dates]
    print(f"   生成信号：{len(signals)} 个，最近交易日：{len(today_signals)} 个")

    if args.dry_run:
        print("\n⚠️ --dry-run：仅扫描不推送")
        for s in today_signals:
            a = "🟢买" if s.action == "buy" else "🔴卖"
            print(f"   {s.date} {a} {s.code} {s.shares}股 @ {s.price:.2f} - {s.reason}")
        return

    pp = get_pushplus()
    if not today_signals:
        pp.send("📊 今日无新信号", f"<p>今日 v3 自适应策略未触发任何买卖信号。</p><p>模式：{gen._mode_series.get(index_kline[-1]['date'] if index_kline else '', 'unknown')}</p>", level="常规")
        return

    for sig in today_signals:
        emoji = "🟢买入" if sig.action == "buy" else "🔴卖出"
        title = f"【{emoji}】{sig.code}"
        content = f"<p>📅 {sig.date}</p><p>📈 {sig.code}</p><p>🎯 ¥{sig.price:.2f}</p><p>📦 {sig.shares}股</p><p>📋 {sig.reason}</p><hr/><p><b>📌 请手动确认后下单</b></p>"
        pp.send(title, content, level="重要")

    pp.send(f"📊 今日信号汇总（{len(today_signals)}个）",
        f"<p>📅 {datetime.now().strftime('%Y-%m-%d')}</p><ol>" +
        "".join(f"<li>{'🟢买' if s.action=='buy' else '🔴卖'} {s.code} {s.shares}股 @ ¥{s.price:.2f}<br/><small>{s.reason}</small></li>" for s in today_signals) +
        "</ol>", level="常规")
    print(f"\n📤 推送完成：{len(today_signals)} 个信号 + 1 个汇总")

if __name__ == "__main__":
    main()
