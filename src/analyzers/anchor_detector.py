"""
B1 锚点判定（bankuai.md v2，S系列阶段B）

自最新交易日向前扫描，返回最近满足任一条件的交易日：
  1) |单日涨跌幅| ≥ big_move_pct（默认 2.0%）
  2) 收盘创 new_high_low_window（默认 20）日新高/新低
  3) 成交量 volume_pctile_window（默认 250）日分位 ≥ volume_pctile（默认 70）

锚点前移置 anchor_shifted=True，下游据此重算 rs_anchor（B2）。

行业层用行业指数历史（ths_cache/industry），概念层用概念指数历史
（ths_cache/concept，新概念 history_limited）。参数源 = config/sector_pool.yaml anchor 块。
"""
import logging
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _anchor_cfg() -> Dict:
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        return cfg.get("anchor", {})
    except Exception:
        return {}


def find_anchor(df: pd.DataFrame, cfg: Optional[Dict] = None) -> Optional[Dict]:
    """扫描板块历史，返回最近锚点。

    Args:
        df: 规范列 DataFrame（trade_date/close/volume），按 trade_date 升序
        cfg: anchor 配置，缺省读 config

    Returns:
        {anchor_date, anchor_condition, anchor_index, anchor_shifted}，
        无足够历史或无锚点返回 None。
    """
    cfg = cfg or _anchor_cfg()
    big_move = float(cfg.get("big_move_pct", 2.0))
    window = int(cfg.get("new_high_low_window", 20))
    vol_pctile = float(cfg.get("volume_pctile", 70))
    vol_window = int(cfg.get("volume_pctile_window", 250))

    if df is None or len(df) < window + 2:
        return None

    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else None
    pct = close.pct_change() * 100.0

    n = len(df)
    for i in range(n - 1, -1, -1):
        conds = []
        # 1) 大波动
        if i > 0 and not pd.isna(pct.iloc[i]) and abs(pct.iloc[i]) >= big_move:
            conds.append("big_move")
        # 2) 20日新高/新低
        lo = max(0, i - window)
        if close.iloc[i] >= close.iloc[lo:i].max() and i - lo >= 1:
            conds.append("new_high")
        elif close.iloc[i] <= close.iloc[lo:i].min() and i - lo >= 1:
            conds.append("new_low")
        # 3) 量分位
        if volume is not None:
            v_lo = max(0, i - vol_window)
            seg = volume.iloc[v_lo:i]
            if len(seg) >= 20 and not seg.empty:
                q = (volume.iloc[i] >= seg.quantile(vol_pctile / 100.0))
                if bool(q):
                    conds.append("volume_pctile")
        if conds:
            return {
                "anchor_date": str(df["trade_date"].iloc[i]),
                "anchor_condition": "+".join(conds),
                "anchor_index": i,
                "anchor_shifted": (i == n - 1),  # 锚点即最新日 → 下游重算 rs_anchor
            }
    return None


def latest_anchor_for_history(df: Optional[pd.DataFrame]) -> Optional[Dict]:
    """便捷入口：读缓存历史 → 找锚点。无数据返回 None（不抛异常）。"""
    if df is None or len(df) < 22:
        return None
    return find_anchor(df)
