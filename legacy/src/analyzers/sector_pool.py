"""
B5 板块池聚合器（bankuai.md v2，S系列阶段B）

流程：
  1) 入围：R1 成交额 Top K（第1周仅开）；R2 5日动量 Top K（第2周加，momentum_enabled）
  2) 合并 anchor 层（portfolio concept_tags ∪ 周报概念名，config，当前为空钩子）
  3) 对池内每个板块算 B1 锚点 / B2 RS / B3 虹吸
  4) mainline 判定：rs_10 前3 ∩ 虹吸(suction_level) 前3 ∩ stage∈{lead,confirm}
  5) 落盘 sector_pool.json（只记录，不出信号）

stage 分类（朴素规则，待回标）：
  lead    rs_5>0 且 rs_10>0 且 suction_state!='releasing'
  confirm rs_10>0 且 (rs_5<=0 或 suction_state=='releasing')
  decline rs_10<=0

口径：成交额排名用当日行业快照（industry_{date}.parquet），指标用 ths_cache 历史。
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg() -> Dict:
    try:
        from src.config_models import load_config
        return load_config("sector_pool.yaml").get("sector_pool", {})
    except Exception:
        return {}


def classify_stage(rs: Optional[Dict], suction: Optional[Dict]) -> str:
    if rs is None or rs.get("rs_10") is None:
        return "unknown"
    r5 = rs.get("rs_5") or 0.0
    r10 = rs.get("rs_10") or 0.0
    ss = (suction or {}).get("suction_state", "stable")
    if r5 > 0 and r10 > 0 and ss != "releasing":
        return "lead"
    if r10 > 0:
        return "confirm"
    return "decline"


def build_pool(date: str, industry_names: List[str]) -> Dict:
    """构建当日板块池。

    Args:
        date: YYYY-MM-DD（读当日行业快照）
        industry_names: 行业宇宙（90 个）
    """
    cfg = _cfg()
    pool_cfg = cfg.get("pool", {})
    r1_k = int(pool_cfg.get("entry_amount_top_k", 60))
    r2_k = int(pool_cfg.get("entry_momentum_top_k", 20))
    momentum_enabled = bool(pool_cfg.get("momentum_enabled", False))

    from src.data_layer.ths_cache import load_history
    from src.analyzers.anchor_detector import find_anchor
    from src.analyzers.sector_rs_tracker import compute_rs
    from src.analyzers.suction_index import state_from_series

    # 当日成交额排名（快照）
    snap_path = os.path.join(PROJECT_ROOT, "data", "sector_snapshots", f"industry_{date}.parquet")
    if os.path.exists(snap_path):
        snap = pd.read_parquet(snap_path)
    else:
        logger.warning("快照 %s 不存在，成交额排名用缓存末行", snap_path)
        snap = None

    # 上证历史（bench）
    index_df = _load_index_history()

    # 全行业成交额矩阵（用于市场占比分母）
    amounts = {}
    for n in industry_names:
        h = load_history("industry", n)
        if h is not None and "amount" in h.columns:
            amounts[n] = h.set_index("trade_date")["amount"]
    mat = pd.DataFrame(amounts).sort_index().fillna(0.0)
    total = mat.sum(axis=1)

    # ---- 入围 ----
    pool_names = set()
    if snap is not None and "总成交额" in snap.columns:
        pool_names |= set(snap.sort_values("总成交额", ascending=False)
                          .head(r1_k)["板块"].tolist())
    if momentum_enabled:
        # R2：5日动量前 K（用缓存 close）
        mom = []
        for n in industry_names:
            h = load_history("industry", n)
            if h is None or len(h) < 6:
                continue
            c = h["close"].astype(float)
            mom.append((n, c.iloc[-1] / c.iloc[-6] - 1.0))
        mom.sort(key=lambda x: x[1], reverse=True)
        pool_names |= {n for n, _ in mom[:r2_k]}
        logger.info("[pool] R2 5日动量前%d 已并入 (enabled=%s)", r2_k, momentum_enabled)
    pool_names &= set(industry_names)
    if not pool_names:
        logger.warning("[pool] %s 入围为空", date)
        return {"date": date, "pool": [], "mainline_industry_top3": [],
                "mainline_industry_list": [], "as_of": date}

    # ---- 算指标 ----
    members = []
    for n in sorted(pool_names):
        h = load_history("industry", n)
        if h is None:
            continue
        anchor = find_anchor(h)
        rs = compute_rs(h, index_df, anchor_date=(anchor or {}).get("anchor_date"))
        # 虹吸：市场占比 = 行业成交额 / 全行业之和（行业层真占比）
        ratio_series = None
        if n in amounts and not total.empty:
            try:
                ratio_series = (amounts[n].reindex(total.index) / total).tolist()
            except Exception:
                ratio_series = None
        suction = state_from_series(ratio_series) if ratio_series else None
        stage = classify_stage(rs, suction)
        members.append({
            "name": n,
            "stage": stage,
            "anchor": anchor,
            "rs": rs,
            "suction": suction,
        })
    if not members:
        return {"date": date, "pool": [], "mainline_industry_top3": [],
                "mainline_industry_list": [], "as_of": date}

    # ---- mainline：rs_10 前3 ∩ 虹吸(suction_level) 前3 ∩ stage∈{lead,confirm} ----
    valid = [m for m in members if m["rs"] and m["rs"].get("rs_10") is not None
             and m["suction"] and m["suction"].get("suction_level") is not None]
    valid.sort(key=lambda m: m["rs"]["rs_10"], reverse=True)
    rs_top3 = {m["name"] for m in valid[:3]}
    valid.sort(key=lambda m: m["suction"]["suction_level"], reverse=True)
    suction_top3 = {m["name"] for m in valid[:3]}
    mainline = [m for m in members
                if m["name"] in rs_top3 and m["name"] in suction_top3
                and m["stage"] in ("lead", "confirm")]

    out = {
        "date": date,
        "as_of": str(mat.index[-1]) if not mat.empty else date,
        "pool": members,
        "mainline_industry_top3": [m["name"] for m in mainline],
        "mainline_industry_list": [m["name"] for m in mainline],
        "pool_size": len(members),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(out, date)
    return out


def _load_index_history() -> Optional[pd.DataFrame]:
    try:
        import akshare as ak
        idx = ak.stock_zh_index_daily(symbol="sh000001")
        idx = idx.rename(columns={"date": "trade_date"})[["trade_date", "close"]]
        idx["trade_date"] = idx["trade_date"].astype(str).str[:10]
        return idx.sort_values("trade_date").reset_index(drop=True)
    except Exception as e:
        logger.warning("上证历史加载失败: %s", e)
        return None


def _save(out: Dict, date: str) -> None:
    d = os.path.join(PROJECT_ROOT, "data", "sector_pool")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"sector_pool_{date}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    logger.info("[pool] 落盘 %s（池 %d 个, mainline %d 个）",
                p, out["pool_size"], len(out["mainline_industry_top3"]))
