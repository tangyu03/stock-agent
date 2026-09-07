"""PushPlus 推送测试"""
import os, sys, logging
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TZ", "Asia/Shanghai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

def main():
    from src.push.pushplus import PushPlus
    print("\n" + "="*60 + "\n  PushPlus 推送测试\n" + "="*60)
    pp = PushPlus()
    print(f"  Token: {pp._token[:8]}...{pp._token[-4:]}")

    tests = [
        ("简单测试", lambda: pp.send("✅ PushPlus 接入测试", "<h3>推送已激活</h3><p>系统信号会自动推送到这里。</p>", level="常规")),
        ("买入信号", lambda: pp.send_entry_signal({"stock_name":"寒武纪","stock_code":"688256","entry_type":"套利低吸","trigger_price":735.05,"stop_loss":714.00,"target_range":[770,800],"position_level":"正常","sector_name":"半导体","sector_status":"主线","note":"测试"})),
        ("卖出信号", lambda: pp.send_exit_signal({"stock_name":"飞沃科技","stock_code":"301232","exit_type":"破位止损","trigger_price":111.67,"stop_loss_price":113.68,"reason":"跌破MA5","urgency":"紧急"})),
        ("做T信号", lambda: pp.send_t0_signal({"stock_name":"超捷股份","stock_code":"301005","signal_type":"正T低吸","direction":"正T","price_range":[105,108],"trigger_price":106.25,"t0_shares":200,"stop_loss_price":104.00,"holding_shares":700,"cost":110.50,"trigger_reason":"竞价换手0.8%+开盘跳水2.5%","time_slot":"09:35"})),
        ("T仓提醒", lambda: pp.send_force_close_remind(content="<h3>今日仍有未了结T仓</h3><p>寒武纪(688256)：未平仓1笔</p><p>📌 铁律：T仓当日必须了结</p>")),
    ]
    for name, fn in tests:
        r = fn()
        print(f"  📨 {name}: {'✅' if r else '❌'}")
    print(f"\n  📊 {sum(1 for _,fn in tests if fn())}/5 成功，剩余 {pp.remaining} 条")

if __name__ == "__main__":
    main()
