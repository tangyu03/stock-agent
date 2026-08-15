"""
S系列 B1/B2/B3/B5 纯逻辑单测 — 无网络，跑：
    python scripts/test_sector_layer.py
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd  # noqa: E402

from src.analyzers.anchor_detector import find_anchor  # noqa: E402
from src.analyzers.sector_rs_tracker import compute_rs  # noqa: E402
from src.analyzers.suction_index import state_from_series  # noqa: E402
from src.analyzers.sector_pool import classify_stage  # noqa: E402

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


def mk_df(closes, volumes=None, start="2026-01-01"):
    """构造规范列 df：60 个交易日 close 升序，可选 volume。"""
    import datetime as dt
    rows = []
    d = dt.date.fromisoformat(start)
    i = 0
    while len(rows) < len(closes):
        if d.weekday() < 5:
            rows.append({"trade_date": d.isoformat()})
        d += dt.timedelta(days=1)
    df = pd.DataFrame(rows[:len(closes)])
    df["close"] = closes
    if volumes:
        df["volume"] = volumes
    return df


def test_anchor():
    print("== B1 find_anchor ==")
    closes = list(range(100, 160))          # 每日+1，末日本身即 20 日新高
    volumes = [100] * 59 + [5000]           # 末日量暴增 → volume_pctile 同时命中
    df = mk_df(closes, volumes)
    a = find_anchor(df, {"big_move_pct": 2.0, "new_high_low_window": 20,
                         "volume_pctile": 70, "volume_pctile_window": 250})
    check("量分位锚点命中末日", a and "volume_pctile" in a["anchor_condition"]
          and a["anchor_index"] == 59 and a["anchor_shifted"] is True)

    closes2 = list(range(100, 160)); closes2[-5] = 200  # index 55 跳涨 → big_move
    df2 = mk_df(closes2, [100 + i % 40 for i in range(60)])  # 量非均匀，避免 volume_pctile 抢命中
    a2 = find_anchor(df2, {"big_move_pct": 2.0, "new_high_low_window": 20,
                           "volume_pctile": 70, "volume_pctile_window": 250})
    # 跳涨(index55)次日 200→156 也是大波动且更近 → 锚点落在 56
    check("大波动锚点(跳涨次日56)", a2 and "big_move" in a2["anchor_condition"]
          and a2["anchor_index"] == 56 and a2["anchor_shifted"] is False)

    df3 = mk_df(list(range(100, 160)))
    a3 = find_anchor(df3, {"big_move_pct": 2.0, "new_high_low_window": 20,
                           "volume_pctile": 70, "volume_pctile_window": 250})
    check("20日新高锚点", a3 and "new_high" in a3["anchor_condition"])

    check("历史不足→None", find_anchor(mk_df(list(range(100, 120))),
          {"big_move_pct": 2.0, "new_high_low_window": 20,
           "volume_pctile": 70, "volume_pctile_window": 250}) is None)


def test_rs():
    print("== B2 compute_rs ==")
    # 板块与上证 21 行同构（step10）→ 任一 n 日累计收益相同 → rs_n ≈ 0
    board = mk_df(list(range(100, 310, 10)))
    index = mk_df(list(range(100, 310, 10)))
    rs = compute_rs(board, index, anchor_date=None)
    check("rs_10 ≈ 0（板块=上证）", rs and abs(rs["rs_10"]) < 0.001)
    check("rs_5 ≈ 0", rs and abs(rs["rs_5"]) < 0.001)
    check("as_of = 板块末日", rs and rs["as_of"] == board["trade_date"].iloc[-1])
    # 板块更强：板块 step15 上证 step10 → rs_10 > 0
    board2 = mk_df(list(range(100, 310, 15)))
    rs2 = compute_rs(board2, index, anchor_date=None)
    check("板块强于上证 → rs_10 > 0", rs2 and rs2["rs_10"] > 0)


def test_suction():
    print("== B3 state_from_series ==")
    s = state_from_series([0.10, 0.11, 0.12, 0.13, 0.14, 0.20],
                          rising=0.30, falling=-0.20, lookback=5)
    check("5日+100% → siphoning", s and s["suction_state"] == "siphoning")
    s2 = state_from_series([0.20, 0.19, 0.18, 0.17, 0.16, 0.10],
                           rising=0.30, falling=-0.20, lookback=5)
    check("5日-50% → releasing", s2 and s2["suction_state"] == "releasing")
    s3 = state_from_series([0.10, 0.10, 0.10, 0.10, 0.10, 0.11],
                           rising=0.30, falling=-0.20, lookback=5)
    check("平稳 → stable", s3 and s3["suction_state"] == "stable")
    check("数据不足 → None", state_from_series([0.1, 0.2]) is None)


def test_stage():
    print("== B5 classify_stage ==")
    check("lead: rs5>0 rs10>0 非releasing",
          classify_stage({"rs_5": 0.03, "rs_10": 0.05}, {"suction_state": "stable"}) == "lead")
    check("confirm: rs10>0 rs5<=0",
          classify_stage({"rs_5": -0.01, "rs_10": 0.05}, {"suction_state": "stable"}) == "confirm")
    check("confirm: rs10>0 但releasing",
          classify_stage({"rs_5": 0.03, "rs_10": 0.05}, {"suction_state": "releasing"}) == "confirm")
    check("decline: rs10<=0",
          classify_stage({"rs_5": -0.01, "rs_10": -0.05}, {"suction_state": "stable"}) == "decline")
    check("数据缺失 → unknown", classify_stage(None, None) == "unknown")


if __name__ == "__main__":
    test_anchor()
    test_rs()
    test_suction()
    test_stage()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
