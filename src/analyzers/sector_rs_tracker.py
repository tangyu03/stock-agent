"""
B2 板块 RS 计算器（bankuai.md v2，S系列阶段B）

双口径并存（纯记录，只加权不出信号）：
  rs_n(t)        = 板块近 n 日累计收益 − 上证近 n 日累计收益，n∈{5,10}
  anchored_excess = 锚点日收盘→当前的板块累计超额收益（对比上证同期）

bench 取上证指数（已有数据，零新增调用）。板块历史来自 ths_cache。

一致性：板块与上证须按 trade_date 对齐（join），不足 n 日返回 None。
"""
import logging
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _rs_cfg() -> Dict:
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        return cfg.get("rs", {})
    except Exception:
        return {}


def _cums(df: pd.DataFrame, n: int) -> Optional[float]:
    """近 n 日累计收益（最新一日往前 n 根，含最新）。不足返回 None。"""
    if df is None or len(df) < n + 1:
        return None
    seg = df["close"].astype(float)
    return float(seg.iloc[-1] / seg.iloc[-1 - n] - 1.0)


def _aligned(board_df: pd.DataFrame, index_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """按 trade_date 内连接对齐板块与上证，取重叠段（从板块侧对齐）。"""
    if board_df is None or index_df is None or board_df.empty or index_df.empty:
        return None
    b = board_df[["trade_date", "close"]].copy()
    i = index_df[["trade_date", "close"]].copy().rename(columns={"close": "index_close"})
    m = b.merge(i, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)
    return m if len(m) >= 11 else None  # 至少够 rs5 + rs10 的最短公共段


def compute_rs(board_df: pd.DataFrame, index_df: pd.DataFrame,
               anchor_date: Optional[str] = None) -> Optional[Dict]:
    """计算板块 RS。

    Args:
        board_df: 板块历史（规范列 trade_date/close）
        index_df: 上证历史（同构）
        anchor_date: B1 锚点日；给则算 anchored_excess

    Returns:
        {rs_5, rs_10, anchored_excess, as_of} 或 None（数据不足）
    """
    cfg = _rs_cfg()
    windows = [int(w) for w in cfg.get("windows", [5, 10])]
    m = _aligned(board_df, index_df)
    if m is None:
        return None

    out: Dict = {"as_of": str(m["trade_date"].iloc[-1])}
    ok = True
    for w in windows:
        if len(m) < w + 1:
            out[f"rs_{w}"] = None
            ok = False
            continue
        b = float(m["close"].iloc[-1] / m["close"].iloc[-1 - w] - 1.0)
        idx = float(m["index_close"].iloc[-1] / m["index_close"].iloc[-1 - w] - 1.0)
        out[f"rs_{w}"] = round(b - idx, 4)

    # 锚点超额：锚点收盘→当前，板块 vs 上证
    out["anchored_excess"] = None
    if anchor_date:
        row = m[m["trade_date"] == anchor_date]
        if not row.empty:
            ai = row.index[0]
            if ai < len(m) - 1:
                b = float(m["close"].iloc[-1] / m["close"].iloc[ai] - 1.0)
                idx = float(m["index_close"].iloc[-1] / m["index_close"].iloc[ai] - 1.0)
                out["anchored_excess"] = round(b - idx, 4)

    out["_complete"] = ok
    return out
