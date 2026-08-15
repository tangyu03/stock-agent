"""
S系列 B4：S3 预期验证盘后评估（bankuai.md v2，Week 1）

读取当日行业一览快照（s1_daily_snapshot 落盘）→ 聚合主线中位数涨幅与全市场涨跌比
→ 取当日市场模式（mode gate，仅 attack 才可能触发）→ 触发判定 → 更新
DivergenceCounter（data_cache 表）。次日盘前由 market_mode_adaptive 读取
计数器执行降级一档。

用法：
    python scripts/s3_daily_eval.py                      # 今天（读取当日行业快照）
    python scripts/s3_daily_eval.py --date 2026-08-14    # 指定日期（回补）
    python scripts/s3_daily_eval.py --mode attack        # 覆盖模式（调试/无K线时）
"""
import argparse
import datetime as dt
import logging
import os
import sys
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_config() -> dict:
    from src.config_models import load_config
    return load_config("sector_pool.yaml").get("sector_pool", {})


def _load_industry_rows(date: str) -> list:
    """优先读当日快照 parquet，缺失则现拉行业一览。"""
    path = os.path.join(PROJECT_ROOT, "data", "sector_snapshots", f"industry_{date}.parquet")
    if os.path.exists(path):
        import pandas as pd
        df = pd.read_parquet(path)
        logger.info("读取快照: %s (%d 行)", path, len(df))
        return df.to_dict("records")
    logger.warning("快照 %s 不存在，现拉行业一览", path)
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    df["trade_date"] = date
    return df.to_dict("records")


def _get_mode(date: str) -> Optional[str]:
    """当日市场模式（attack/defend/retreat）。拉取失败返回 None（mode gate 不触发）。"""
    try:
        from src.loop.market_mode_adaptive import get_market_mode_adaptive
        mma = get_market_mode_adaptive()
        kline = mma._fetch_index_kline()
        return mma.get_mode_for_date(date, kline)
    except Exception as e:
        logger.warning("市场模式获取失败: %s", e)
        return None


def run(date: Optional[str] = None, mode_override: Optional[str] = None) -> dict:
    d = date or dt.date.today().isoformat()
    cfg = _load_config()
    ed = cfg.get("expectation_divergence", {})
    proxy = cfg.get("mainline_proxy", {})

    from src.analyzers.expectation_divergence import (
        DivergenceCounter, compute_indicators, mainline_proxy, triggered)

    rows = _load_industry_rows(d)
    mainline = mainline_proxy(rows, k=proxy.get("k", 5), by=proxy.get("by", "总成交额"))
    indicators = compute_indicators(rows, mainline)
    mode = mode_override or _get_mode(d)
    trig = triggered(
        indicators, mode,
        median_chg_threshold=ed.get("median_chg", 0.0),
        ad_ratio_threshold=ed.get("ad_ratio", 0.8))

    counter = DivergenceCounter.load(
        consecutive_threshold=ed.get("consecutive_days", 2))
    counter.record(d, trig)
    counter.save()

    logger.info(
        "[S3] %s mode=%s 主线=%s 中位涨幅=%s 涨跌比=%s 触发=%s → 连续%d日(阈值%d)",
        d, mode, mainline,
        None if indicators is None else round(indicators["mainline_median_chg"], 2),
        None if indicators is None else round(indicators["ad_ratio"], 3),
        trig, counter.consecutive_days, counter.consecutive_threshold)
    return {
        "date": d, "mode": mode, "mainline": mainline,
        "indicators": indicators, "triggered": trig,
        "consecutive_days": counter.consecutive_days,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="S3 预期验证盘后评估")
    p.add_argument("--date", default=None, help="评估日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--mode", default=None, help="覆盖市场模式（调试用）")
    a = p.parse_args()
    run(a.date, a.mode)
