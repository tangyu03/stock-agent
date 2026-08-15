"""
S系列 A2：THS 板块历史初始化（bankuai.md v2，一次性回填）

全量回填 90 行业指数 + config 跟踪子集概念指数（现 6 个），2020-01-01 起，
落盘 data/ths_cache/{industry,concept}/<name>.parquet（规范列）+ manifest.json。
复用"分批+断点续传"：已落盘且非 --force 则跳过。

⚠️ 陷阱①硬约束：行业指数默认 end_date='20240108'、概念指数默认截在 '20250228'，
本脚本一律显式传 end_date=今天，否则静默截断。

用法：
    python scripts/s2_history_init.py                 # 全量（断点续传）
    python scripts/s2_history_init.py --force         # 强制重建
    python scripts/s2_history_init.py --type industry # 仅行业
    python scripts/s2_history_init.py --type concept  # 仅概念
"""
import argparse
import datetime as dt
import logging
import os
import sys
from typing import List

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

START_DATE = "20200101"  # 成熟板块 2020 起，新板块从成立日起（接口自动给）

_HAS_PARQUET = True
try:
    import pyarrow  # noqa: F401
except ImportError:
    _HAS_PARQUET = False


def _today() -> str:
    return dt.date.today().strftime("%Y%m%d")


def _industry_universe() -> List[str]:
    import akshare as ak
    df = ak.stock_board_industry_name_ths()
    return df[df.columns[0]].tolist()


def _concept_tracklist() -> List[str]:
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        names = cfg.get("concept_tracklist") or []
        max_n = int(cfg.get("max_tracked_concepts", 30))
        return list(names)[:max_n]
    except Exception as e:
        logger.warning("读取跟踪子集失败(%s)，用内置兜底", e)
        return ["共封装光学(CPO)", "液冷服务器", "存储芯片", "MLCC概念", "算力租赁", "PCB概念"]


def _fetch(kind: str, symbol: str, end: str) -> pd.DataFrame:
    import akshare as ak
    if kind == "industry":
        return ak.stock_board_industry_index_ths(symbol=symbol, start_date=START_DATE, end_date=end)
    return ak.stock_board_concept_index_ths(symbol=symbol, start_date=START_DATE, end_date=end)


def _save(kind: str, name: str, df: pd.DataFrame) -> str:
    from src.data_layer.ths_cache import cache_path, canonicalize
    df = canonicalize(df)
    p = cache_path(kind, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_PARQUET:
        df.to_parquet(p)
    else:
        p = p.with_suffix(".csv")
        df.to_csv(p, index=False)
    return str(p)


def run(types: List[str], force: bool = False) -> dict:
    from src.data_layer.ths_cache import _summary_entry, load_manifest, save_manifest
    end = _today()
    manifest = load_manifest()
    stats = {"industry": {"ok": 0, "skip": 0, "fail": 0},
             "concept": {"ok": 0, "skip": 0, "fail": 0}}

    for kind in types:
        universe = _industry_universe() if kind == "industry" else _concept_tracklist()
        logger.info("[A2] 开始回填 %s 共 %d 个", kind, len(universe))
        for i, name in enumerate(universe, 1):
            entry = manifest.get(name, {})
            if not force and entry.get("type") == kind:
                stats[kind]["skip"] += 1
                continue  # 断点续传：已落盘且无新元数据
            try:
                df = _fetch(kind, name, end)
                if df is None or len(df) == 0:
                    raise ValueError("空数据")
                p = _save(kind, name, df)
                manifest[name] = _summary_entry(name, kind, df)
                stats[kind]["ok"] += 1
                logger.info("[A2] %3d/%d %s [%d行 → %s] %s",
                            i, len(universe), name, len(df), df['trade_date'].iloc[-1], p.split(os.sep)[-1])
            except Exception as e:
                stats[kind]["fail"] += 1
                logger.warning("[A2] 失败 %s: %s", name, str(e)[:120])
        save_manifest(manifest)

    logger.info("[A2] 完成: %s", stats)
    return stats


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="S系列 THS 历史初始化（一次性回填）")
    p.add_argument("--force", action="store_true", help="强制重建（忽略已有缓存）")
    p.add_argument("--type", choices=["industry", "concept", "all"], default="all")
    a = p.parse_args()
    types = ["industry", "concept"] if a.type == "all" else [a.type]
    run(types, a.force)
