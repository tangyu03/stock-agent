"""
S3 预期验证（B4）纯逻辑单测 — 无网络，跑：
    python scripts/test_s3_divergence.py
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.analyzers.expectation_divergence import (  # noqa: E402
    DivergenceCounter, compute_indicators, downgrade_one_notch,
    mainline_proxy, triggered)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


# ---------- 合成行业快照（10 行业，5 强主线 5 弱） ----------
def make_rows(mainline_chg=-1.5, up=700, down=1200, amount=None):
    # 前5个（半导体..传媒）是"资金所在"的弱主线：成交额最高（1000递减）→ mainline_proxy 选中它们
    names = ["半导体", "计算机", "通信", "电子", "传媒", "银行", "煤炭", "地产", "保险", "电力"]
    rows = []
    for i, n in enumerate(names):
        rows.append({
            "板块": n,
            "涨跌幅": mainline_chg if n in ("半导体", "计算机", "通信", "电子", "传媒") else 1.0,
            "总成交额": amount if amount else (1000 - i * 10),
            "上涨家数": up,
            "下跌家数": down,
        })
    return rows


def test_indicators():
    print("== compute_indicators / mainline_proxy / triggered ==")
    rows = make_rows()
    mainline = mainline_proxy(rows, k=5)  # 成交额前5 = 半导体..传媒（amount 递增，前5是后5? 检查）
    ind = compute_indicators(rows, mainline)
    check("mainline_proxy 返回5个", len(mainline) == 5)
    check("indicators 非空", ind is not None)
    # 涨跌比 = 10*700 / 10*1200 = 0.583 < 0.8；主线中位涨幅 -1.5 < 0
    check("ad_ratio 计算正确", abs(ind["ad_ratio"] - 0.5833) < 0.01)
    check("主线中位涨幅为负", ind["mainline_median_chg"] < 0)
    # 触发：mode=attack 且 两条件全中
    check("attack+弱主线+低涨跌比 → 触发", triggered(ind, "attack") is True)
    check("defend → 不触发（mode gate）", triggered(ind, "defend") is False)
    check("retreat → 不触发", triggered(ind, "retreat") is False)


def test_not_triggered_when_strong():
    print("== 主线不弱 / 涨跌比不低 → 不触发 ==")
    rows = make_rows(mainline_chg=1.5, up=1200, down=700)  # ad_ratio=1.71
    ind = compute_indicators(rows, mainline_proxy(rows, k=5))
    check("主线走强 → 不触发", triggered(ind, "attack") is False)
    rows2 = make_rows(mainline_chg=-1.5, up=1200, down=700)  # 主线弱但涨跌比高
    ind2 = compute_indicators(rows2, mainline_proxy(rows2, k=5))
    check("涨跌比≥0.8 → 不触发", triggered(ind2, "attack") is False)


def test_downgrade():
    print("== downgrade_one_notch ==")
    check("attack→defend", downgrade_one_notch("attack") == "defend")
    check("defend→retreat", downgrade_one_notch("defend") == "retreat")
    check("retreat 不动", downgrade_one_notch("retreat") == "retreat")


def test_counter_consecutive():
    print("== DivergenceCounter 连续天数（内存版，不碰库） ==")
    c = DivergenceCounter(consecutive_threshold=2)
    c.record("2026-08-14", True)   # 周五触发
    check("首日触发 → 1", c.consecutive_days == 1 and c.last_trigger_date == "2026-08-14")
    c.record("2026-08-17", True)   # 周一（下一交易日）触发 → 连续
    check("跨周末连续 → 2", c.consecutive_days == 2)
    c.record("2026-08-18", False)  # 周二不触发 → 清零
    check("不触发 → 清零", c.consecutive_days == 0 and c.last_trigger_date is None)
    c.record("2026-08-19", True)
    c.record("2026-08-20", True)
    check("连续2日 → 达标", c.should_downgrade("2026-08-21") is True)
    c.consume("2026-08-21")
    check("同日 consume → 幂等不再降", c.should_downgrade("2026-08-21") is False)
    check("次日可再降", c.should_downgrade("2026-08-22") is True)


def test_counter_gap():
    print("== 隔日缺口不连续 ==")
    c = DivergenceCounter(consecutive_threshold=2)
    c.record("2026-08-14", True)
    c.record("2026-08-18", True)  # 18 的上一交易日是 17，不是 14 → 不连续
    check("隔日触发 → 重置为1", c.consecutive_days == 1)


if __name__ == "__main__":
    test_indicators()
    test_not_triggered_when_strong()
    test_downgrade()
    test_counter_consecutive()
    test_counter_gap()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
