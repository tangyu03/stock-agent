"""
THS 板块历史缓存读写层（S系列 A2 落盘 + B1/B2/B3/B5 消费统一入口）

落盘规范（scripts/s2_history_init.py 写入）：
  data/ths_cache/industry/<name>.parquet    # 行业指数历史（90）
  data/ths_cache/concept/<name>.parquet     # 概念指数历史（跟踪子集）
  data/ths_cache/manifest.json              # {name: {type, rows, first_date, last_date,
                                            #    history_limited, updated_at}}

统一列名（下游消费约定）：
  trade_date / open / high / low / close / volume / amount

⚠️ 数据源静默截断（陷阱①，bankuai.md）：THS 行业指数默认 end_date='20240108'、
概念指数默认截在 '20250228'。任何回填必须显式传 end_date=今天；本层回读只认显式
落盘的数据，不背"没传日期"的锅。
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "ths_cache"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

# THS 原始列名 → 规范列名
_COL_MAP = {
    "日期": "trade_date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "成交额": "amount",
    # 兼容行业一览等其它形态
    "板块": "sector",
    "涨跌幅": "pct_chg",
}


def min_history_days() -> int:
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        return int(cfg.get("data", {}).get("min_history_days", 120))
    except Exception:
        return 120


def cache_path(kind: str, name: str) -> Path:
    return CACHE_DIR / kind / f"{name}.parquet"


def canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """THS 原始列 → 规范列（trade_date 统一为字符串 YYYY-MM-DD）。"""
    df = df.rename(columns=_COL_MAP)
    keep = [c for c in ("trade_date", "open", "high", "low", "close",
                        "volume", "amount") if c in df.columns]
    df = df[keep].copy()
    if "trade_date" in df.columns:
        df["trade_date"] = df["trade_date"].astype(str).str[:10]
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("trade_date").reset_index(drop=True)


def load_history(kind: str, name: str) -> Optional[pd.DataFrame]:
    """读行业/概念历史（规范列），无缓存返回 None。"""
    p = cache_path(kind, name)
    if not p.exists():
        return None
    try:
        return canonicalize(pd.read_parquet(p))
    except Exception as e:
        logger.warning("缓存读取失败 %s: %s", p, e)
        return None


# ---------------- manifest ----------------
def load_manifest() -> Dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("manifest 读取失败: %s", e)
    return {}


def save_manifest(manifest: Dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def _summary_entry(name: str, kind: str, df: pd.DataFrame) -> Dict:
    rows = len(df)
    return {
        "type": kind,
        "rows": rows,
        "first_date": str(df["trade_date"].iloc[0]) if rows else None,
        "last_date": str(df["trade_date"].iloc[-1]) if rows else None,
        "history_limited": rows < min_history_days(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def rebuild_manifest() -> Dict:
    """从磁盘已有 parquet 重建 manifest（manifest 丢失/损坏时，无需重拉数据）。"""
    manifest = {}
    for kind in ("industry", "concept"):
        d = CACHE_DIR / kind
        if not d.exists():
            continue
        for p in d.glob("*.parquet"):
            try:
                df = canonicalize(pd.read_parquet(p))
                manifest[p.stem] = _summary_entry(p.stem, kind, df)
            except Exception as e:
                logger.warning("manifest 重建跳过 %s: %s", p, e)
    save_manifest(manifest)
    logger.info("manifest 重建完成: %d 条", len(manifest))
    return manifest
