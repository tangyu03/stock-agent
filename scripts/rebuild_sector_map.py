"""
板块映射快照重建脚本（Step1 独立入口，手动/定时触发）

用法：
    python -m scripts.rebuild_sector_map                    # 重建今天
    python -m scripts.rebuild_sector_map --date 2026-08-22  # 指定交易日
    python -m scripts.rebuild_sector_map --force            # 覆盖当天已有快照
    python -m scripts.rebuild_sector_map --prune            # 构建后清理过期快照
    python -m scripts.rebuild_sector_map --check            # 仅检查各天快照状态
    python -m scripts.rebuild_sector_map --scope watchlist  # 覆盖 config 扫描范围

数据流（Step1）：
  东财 push2 板块排名(1次API) → datacenter 全量 A 股行业归属(不反爬) → cons_em 补缺
  → K线轻量指标(优先 ths_cache 历史) → BoardSnapshot → 落 board_snapshot/board_component
"""
import sys
import os
import logging
import argparse
from datetime import date, datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def check(store, max_stale_days: int):
    """仅检查快照状态：各天快照存在/规模/滞后天数"""
    from src.db import get_connection
    conn = get_connection()
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT snapshot_date, COUNT(*) AS sector_count, SUM(stock_count) AS stock_total "
        "FROM board_snapshot GROUP BY snapshot_date ORDER BY snapshot_date DESC LIMIT 15"
    ).fetchall()
    print("=" * 70)
    print(f"  板块快照状态（今天 {today}）")
    print("=" * 70)
    if not rows:
        print("  无任何快照（尚未构建）")
        return
    for r in rows:
        d = r["snapshot_date"]
        lag = (date.fromisoformat(today) - date.fromisoformat(d)).days
        flag = "✓ 今天" if lag == 0 else (f"stale {lag}天" if 0 < lag <= max_stale_days else "✗ 过期")
        print(f"  {d}  板块 {r['sector_count']:>3}  成分股归属 {r['stock_total']:>6}  [{flag}]")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="板块映射快照重建（Step1）")
    parser.add_argument("--date", type=str, default=None, help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--force", action="store_true", help="覆盖当天已有快照")
    parser.add_argument("--prune", action="store_true", help="构建后清理过期快照")
    parser.add_argument("--check", action="store_true", help="仅检查快照状态")
    parser.add_argument("--scope", type=str, default=None, help="覆盖 config 扫描范围")
    args = parser.parse_args()

    from src.cache import get_sector_map_service
    service = get_sector_map_service()

    if args.check:
        check(service.store, service._stale_max_days())
        return

    # 指定日期
    d = args.date or date.today().isoformat()
    try:
        target = date.fromisoformat(str(d)[:10])
    except ValueError:
        logger.error("无效日期: %s", d)
        sys.exit(2)

    # 非交易日跳过（周末；与 s1_daily_snapshot 一致）
    if target.weekday() >= 5:
        logger.info("非交易日（周末），跳过构建 %s", target.isoformat())
        sys.exit(0)

    # 覆盖 scope（若命令行指定）
    if args.scope:
        service.cfg["scope"] = args.scope

    # 幂等：已存在且非 force → 跳过
    if not args.force and service.store.has(target.isoformat()):
        logger.info("快照 %s 已存在（--force 强制重建）", target.isoformat())
        check(service.store, service._stale_max_days())
        sys.exit(0)

    logger.info("开始构建板块快照 %s ...", target.isoformat())
    try:
        snap = service.build_snapshot(target.isoformat(), force=args.force)
    except Exception as e:
        logger.error("板块快照构建失败: %s", e)
        sys.exit(1)

    logger.info("构建完成: %d 个板块, %d 只成分股归属", len(snap.sectors), len(snap.stock_to_sectors))

    if args.prune:
        removed = service.store.prune(service._retention_days())
        logger.info("清理过期快照: %d 天", removed)

    # 覆盖率诊断：watchlist 命中数
    try:
        from src.config_models import load_config
        portfolio = load_config("portfolio.yaml")
        codes = [s.get("code", "") for s in (portfolio.get("stocks") or []) if s.get("code")]
        if codes:
            stock_sectors = service.store.load_stock_sectors(snap.snapshot_date, codes)
            hit = sum(1 for v in stock_sectors.values() if v)
            logger.info("watchlist 覆盖率: %d/%d 只命中板块归属", hit, len(codes))
    except Exception:
        pass


if __name__ == "__main__":
    main()
