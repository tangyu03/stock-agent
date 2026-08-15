"""
S系列阶段A：每日板块快照落盘（bankuai.md v2）

日稳态调用 = 行业一览 1 次 + 概念指数 N 次（跟踪子集，默认 ≤30）≈ 31 次/日。
所有快照 parquet 强制带 trade_date 字段，喂给 DataFreshnessGuard。
非交易日自动跳过（F9 周末判断）。盘后固定时点（16:00/17:00）由 orchestrator 调度。

数据源全部为同花顺 THS（本环境实测可用，2026-08-15 验证）：
  - 行业一览 stock_board_industry_summary_ths()          # 90 行业，实时快照
  - 概念指数 stock_board_concept_index_ths(name, start, end)
    ※ 必须显式传日期，否则 akshare 默认静默截断在 2025-02-28（陷阱①，见 bankuai.md）

用法：
    python -m scripts.s1_daily_snapshot                      # 今天（非交易日自动跳过）
    python -m scripts.s1_daily_snapshot --date 2026-08-14    # 指定日期
    python -m scripts.s1_daily_snapshot --concepts CPO 液冷服务器   # 覆盖子集（调试）
"""
import argparse
import datetime as dt
import logging
import os
import sys
from typing import List

import pandas as pd

# 加项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_HAS_PARQUET = True
try:
    import pyarrow  # noqa: F401
except ImportError:
    _HAS_PARQUET = False

DEFAULT_TRACKLIST = ["CPO", "液冷服务器", "存储芯片", "MLCC",
                     "CXO", "算力租赁", "AI智能营销", "PCB"]


def _load_tracklist() -> List[str]:
    """从 config/sector_pool.yaml 读跟踪子集，读失败用内置兜底。"""
    try:
        from src.config_models import load_config
        cfg = load_config("sector_pool.yaml").get("sector_pool", {})
        names = cfg.get("concept_tracklist") or []
        max_n = int(cfg.get("max_tracked_concepts", 30))
        return list(names)[:max_n]
    except Exception as e:
        logger.warning("读取 sector_pool.yaml 失败(%s)，用内置兜底子集", e)
        return list(DEFAULT_TRACKLIST)


def fetch_industry_snapshot(trade_date: str) -> pd.DataFrame:
    """行业一览：90 行业，含总成交额/涨跌家数/领涨股。强制附 trade_date。"""
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    df["trade_date"] = trade_date
    return df


def fetch_concept_rows(names: List[str], trade_date: str) -> List[dict]:
    """概念指数：跟踪子集各取 ≤trade_date 的最新一行（收盘+成交量+成交额）。

    拉取窗口为 [trade_date-7d, trade_date]（而非 start==end 单日），避免 akshare
    对 start==end 的边界问题，也容忍概念指数当日未更新的滞后；返回行实际日期
    可能 < trade_date，由 DataFreshnessGuard 负责报警。单概念失败登记不中断。
    """
    import akshare as ak
    start = (dt.date.fromisoformat(trade_date) - dt.timedelta(days=7)).isoformat()
    rows = []
    for nm in names:
        try:
            df = ak.stock_board_concept_index_ths(
                symbol=nm, start_date=start, end_date=trade_date)
            if df is None or len(df) == 0:
                logger.warning("[snapshot] 概念 %s 窗口[%s,%s]无数据", nm, start, trade_date)
                continue
            last = df.iloc[-1]
            rows.append({
                "concept": nm,
                "trade_date": str(last.get("日期", trade_date)),
                "close": float(last.get("收盘价", last.get("收盘", 0)) or 0),
                "volume": float(last.get("成交量", 0) or 0),
                "amount": float(last.get("成交额", 0) or 0),
            })
        except Exception as e:
            logger.warning("[snapshot] 概念 %s 拉取失败: %s", nm, str(e)[:120])
    return rows


def _save(df: pd.DataFrame, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if _HAS_PARQUET:
        df.to_parquet(path)
    else:
        path = path.rsplit(".", 1)[0] + ".csv"  # parquet 不可用时降级 csv
        df.to_csv(path, index=False)
    logger.info("已落盘: %s (%d 行)", path, len(df))
    return path


def run(trade_date=None, tracklist: List[str] | None = None) -> List[str]:
    """执行一次快照，返回落盘路径列表。非交易日返回空列表。"""
    d = trade_date or dt.date.today().isoformat()
    try:
        if dt.date.fromisoformat(d).weekday() >= 5:  # 周六日
            logger.info("[snapshot] %s 周末非交易日，跳过", d)
            return []
    except ValueError:
        logger.error("[snapshot] 非法日期: %s", d)
        return []

    out_dir = os.environ.get(
        "SECTOR_SNAPSHOT_DIR",
        os.path.join(PROJECT_ROOT, "data", "sector_snapshots"))
    paths = []

    # 1) 行业一览（1 次调用）
    try:
        ind = fetch_industry_snapshot(d)
        if len(ind):
            paths.append(_save(ind, os.path.join(out_dir, f"industry_{d}.parquet")))
    except Exception as e:
        logger.error("[snapshot] 行业一览失败: %s", e)

    # 2) 概念指数（跟踪子集 N 次调用）
    names = tracklist or _load_tracklist()
    rows = fetch_concept_rows(names, d)
    if rows:
        paths.append(_save(pd.DataFrame(rows), os.path.join(out_dir, f"concept_{d}.parquet")))

    logger.info("[snapshot] 完成: %d 个文件, 概念 %d/%d", len(paths), len(rows), len(names))
    return paths


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="S系列每日板块快照（THS源）")
    p.add_argument("--date", default=None, help="快照日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--concepts", nargs="*", default=None, help="覆盖跟踪子集（调试用）")
    a = p.parse_args()
    run(a.date, a.concepts)
