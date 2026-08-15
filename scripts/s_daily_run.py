"""
S系列 每日一体化运行（盘后调度入口，bankuai.md v2 阶段A/B）

单条命令跑完整日管道：
  1) A1 每日快照（行业一览1 + 概念指数N）→ s1_daily_snapshot
  2) DataFreshnessGuard 主网断言（最后日期==最近交易日，不满足→剔除+告警）
  3) B4 S3 盘后评估（更新 DivergenceCounter）→ s3_daily_eval
  4) B5 板块池聚合（入围+指标+mainline）→ sector_pool

盘后固定时点（16:00/17:00）由外部调度（Windows 任务计划/或长驻循环）调本脚本。
非交易日（周末）自动跳过。

用法：
    python scripts/s_daily_run.py                  # 今天
    python scripts/s_daily_run.py --date 2026-08-14
    python scripts/s_daily_run.py --skip-snapshot  # 跳过拉取，仅评估已有快照（重放）
"""
import argparse
import datetime as dt
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run(date=None, skip_snapshot: bool = False) -> dict:
    d = date or dt.date.today().isoformat()
    # 非交易日跳过（周末；假期由守卫告警而非静默）
    try:
        if dt.date.fromisoformat(d).weekday() >= 5:
            logger.info("[daily] %s 周末非交易日，跳过", d)
            return {"date": d, "skipped": True}
    except ValueError:
        logger.error("[daily] 非法日期 %s", d)
        return {"date": d, "error": "bad_date"}

    summary = {"date": d, "skipped": False}

    # ---- 1) 快照 ----
    if skip_snapshot:
        logger.info("[daily] --skip-snapshot：跳过拉取")
    else:
        from scripts.s1_daily_snapshot import run as snapshot_run
        paths = snapshot_run(d)
        summary["snapshot_files"] = paths

    # ---- 2) 新鲜度守卫（行业快照主网） ----
    import pandas as pd
    from src.loop.data_freshness import DataFreshnessGuard
    guard = DataFreshnessGuard()
    snap_p = os.path.join(PROJECT_ROOT, "data", "sector_snapshots", f"industry_{d}.parquet")
    if os.path.exists(snap_p):
        rows = pd.read_parquet(snap_p).to_dict("records")
        fresh = guard.check_source("industry_summary", rows)
        summary["freshness_ok"] = fresh
        if not fresh:
            logger.error("[daily] 行业快照不新鲜 → 当日该源剔除计算（告警通道见实盘推送）")
    else:
        summary["freshness_ok"] = None
        logger.warning("[daily] 行业快照缺失，守卫跳过")

    # ---- 3) S3 盘后评估 ----
    try:
        from scripts.s3_daily_eval import run as s3_run
        s3 = s3_run(d)
        summary["s3"] = {"triggered": s3.get("triggered"),
                         "mode": s3.get("mode"),
                         "consecutive_days": s3.get("consecutive_days")}
    except Exception as e:
        summary["s3"] = {"error": str(e)[:120]}
        logger.error("[daily] S3 评估失败: %s", e)

    # ---- 4) B5 板块池 ----
    try:
        from src.analyzers.sector_pool import build_pool
        industry_names = sorted(
            n[:-8] for n in os.listdir(os.path.join(PROJECT_ROOT, "data", "ths_cache", "industry")))
        pool = build_pool(d, industry_names)
        summary["pool_size"] = pool.get("pool_size")
        summary["mainline"] = pool.get("mainline_industry_top3")
    except Exception as e:
        summary["pool"] = {"error": str(e)[:120]}
        logger.error("[daily] B5 池构建失败: %s", e)

    logger.info("[daily] 完成: %s", {k: v for k, v in summary.items() if k != "date"})
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="S系列每日一体化运行")
    p.add_argument("--date", default=None)
    p.add_argument("--skip-snapshot", action="store_true")
    a = p.parse_args()
    run(a.date, a.skip_snapshot)
