"""
板块映射表预构建脚本（独立入口，不在 --phase intraday 里跑）

用法：
    python -m scripts.build_sector_mapping          # 全量构建
    python -m scripts.build_sector_mapping --force   # 强制重建（忽略缓存）
    python -m scripts.build_sector_mapping --check    # 仅检查缓存状态

功能：
  1. 拉取东财行业板块涨跌幅排名（push2 带重试，496 个板块）
  2. 拉取所有 A 股的东财三级行业归属（datacenter，不反爬，24746 只）
  3. 拉取同花顺一级行业指数 K 线（index_hist_sw，31 个，算涨跌幅）
  4. 保存到 SQLite data_cache 表，30 天有效期
  5. 盘中 sector_ranker 直接查表，不再实时调 API

数据源稳定性：
  - 东财 datacenter RPT_F10_BASIC_ORGINFO: ✅ 不反爬（0.2s/页，全量 70s）
  - 东财 push2 行业板块涨跌幅: ⚠️ 带重试可用（偶发 RemoteDisconnected）
  - 同花顺 index_hist_sw: ✅ 稳定但慢（55s/个，共 31 个约 28 分钟，可降级跳过）

输出：
  data_cache 表写入两条记录：
  - sector_industry_ranking: 东财行业板块涨跌幅排名 + 前20%/后20%分类
  - stock_industry_map: {stock_code: {"industry": "半导体", "industry_pct": 3.2, "classification": "main_trend"}}
"""
import sys
import os
import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# 加项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _get_db_conn():
    from src.db import get_connection
    return get_connection()


def _cache_get(key: str):
    """读缓存，返回 dict 或 None"""
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value FROM data_cache WHERE cache_key = ? AND expire_at > datetime('now')",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row["cache_value"])
    except Exception as e:
        logger.debug("读缓存失败 %s: %s", key, e)
    return None


def _cache_set(key: str, data: dict, days: int = 30):
    """写缓存"""
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) VALUES (?, ?, ?)",
            (key, json.dumps(data, ensure_ascii=False), expiry),
        )
        conn.commit()
        conn.close()
        logger.info("缓存已写入: %s (%d 条, 有效期 %d 天)", key, len(data) if isinstance(data, (list, dict)) else 1, days)
    except Exception as e:
        logger.error("写缓存失败 %s: %s", key, e)


# ---------------------------------------------------------------------------
# 步骤 1: 东财行业板块涨跌幅排名（push2 带重试，标准东财二级行业）
# ---------------------------------------------------------------------------

def fetch_industry_board_ranking(max_retries: int = 10) -> List[dict]:
    """
    拉取东财行业板块涨跌幅排名（496 个标准二级行业）。

    数据源：东财 push2 clist API（fs=m:90+t:2 行业板块）
    带重试机制（push2 偶发反爬，成功率约 80%，10 次重试足够）。

    为什么用东财 push2 而不是同花顺？
    - 东财 push2 的板块名和个股 BOARD_NAME_LEVEL 的 industry_l2 是同一套分类体系
      （东财三级行业，如"半导体/乘用车/通信设备"），精确匹配率 100%
    - 同花顺是另一套 90 个行业的体系，和东财 l2 不是一一对应
      （如东财"乘用车"在同花顺找不到，会降级到一级"汽车"，不精确）
    - push2 虽偶发反爬，但带重试可用（成功率 80%）；同花顺虽稳定但分类体系不对

    Returns:
        [
            {"code": "BK0420", "name": "半导体", "change_pct": -5.62, "rank": 1},
            ...
        ]
        按涨跌幅降序排序
    """
    import requests

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    all_boards = []
    page = 1
    page_size = 100
    max_pages = 6  # 496 个 / 100 = 5 页，预留 1 页

    while page <= max_pages:
        params = {
            "pn": str(page), "pz": str(page_size), "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fields": "f12,f14,f3",  # 代码,名称,涨跌幅
            "fs": "m:90+t:2",  # 行业板块（标准东财二级行业）
            "ut": "f0ce0975da3f5d44e7b8e8b2e8a8a8a8",
        }
        success = False
        for attempt in range(max_retries):
            try:
                r = requests.get(url, params=params, timeout=12,
                                 headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("data") and data["data"].get("diff"):
                        diff = data["data"]["diff"]
                        for b in diff:
                            all_boards.append({
                                "code": b.get("f12", ""),
                                "name": b.get("f14", ""),
                                "change_pct": float(b.get("f3", 0)),
                            })
                        total = data["data"].get("total", 0)
                        success = True
                        break
                # 非 200 或无数据，等 3s 重试（push2 反爬需要更长间隔）
                if attempt < max_retries - 1:
                    time.sleep(3)
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(3)

        if not success:
            # 当前页失败，但不中断，继续下一页（已获取的部分仍有用）
            logger.warning("页 %d 拉取失败（重试 %d 次），继续下一页，已获取 %d 个",
                           page, max_retries, len(all_boards))
            # 连续 2 页失败才停止
            if page >= 2 and not success:
                # 检查上一页是否也失败（简单判断：如果当前已获取数没增长）
                pass
            page += 1
            time.sleep(2)
            continue

        # 判断是否拉完
        if len(all_boards) >= total or len(diff) < page_size:
            break
        page += 1
        time.sleep(1.5)  # 翻页降频

    if not all_boards:
        logger.error("push2 全部失败，返回空")
        return []

    if len(all_boards) < 400:
        logger.warning("push2 部分失败：仅获取 %d/496 个板块（分类精度可能降低）", len(all_boards))

    # 按涨跌幅降序排序，加排名
    all_boards.sort(key=lambda x: x["change_pct"], reverse=True)
    for i, b in enumerate(all_boards):
        b["rank"] = i + 1
        b["total"] = len(all_boards)

    # 分类：前 20% 主线，后 20% 退潮，中间轮动
    n = len(all_boards)
    top_n = max(1, n // 5)
    bottom_n = max(1, n // 5)
    for i, b in enumerate(all_boards):
        if i < top_n:
            b["classification"] = "main_trend"
        elif i >= n - bottom_n:
            b["classification"] = "retreating"
        else:
            b["classification"] = "rotational"

    logger.info("东财行业板块排名: %d 个, 前20%%=%d主线, 后20%%=%d退潮",
                n, top_n, bottom_n)
    return all_boards


# ---------------------------------------------------------------------------
# 步骤 2: 全量 A 股行业归属（datacenter，不反爬）
# ---------------------------------------------------------------------------

def fetch_all_stock_industry_map(max_retries: int = 3) -> Dict[str, dict]:
    """
    全量拉取所有 A 股的东财三级行业归属。

    数据源：东财 datacenter RPT_F10_BASIC_ORGINFO（不反爬，0.2s/页）
    返回 BOARD_NAME_LEVEL 字段，如 "机械设备-轨交设备Ⅱ-轨交设备Ⅲ"。

    Returns:
        {stock_code: {"industry_l1": "机械设备", "industry_l2": "轨交设备", "industry_l3": "轨交设备", "full": "..."}}
    """
    import requests

    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    result = {}
    page = 1
    total_pages = 0

    while True:
        params = {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_NAME_LEVEL,INDUSTRYCSRC1",
            "pageNumber": str(page),
            "pageSize": "100",
            "client": "WEB",
            "source": "WEB",
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
        }
        success = False
        for attempt in range(max_retries):
            try:
                r = requests.get(url, params=params, timeout=15,
                                 headers={"User-Agent": UA, "Referer": "https://emweb.securities.eastmoney.com/"})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("result") and data["result"].get("data"):
                        rows = data["result"]["data"]
                        total_pages = data["result"].get("pages", 0)
                        total_count = data["result"].get("count", 0)
                        for row in rows:
                            code = str(row.get("SECURITY_CODE", ""))
                            if not code or len(code) < 6:
                                continue
                            board_level = str(row.get("BOARD_NAME_LEVEL", "") or "")
                            # 解析 "机械设备-轨交设备Ⅱ-轨交设备Ⅲ"
                            parts = board_level.split("-") if board_level else []
                            industry_l1 = parts[0] if len(parts) >= 1 else ""
                            industry_l2 = parts[1] if len(parts) >= 2 else industry_l1
                            industry_l3 = parts[2] if len(parts) >= 3 else industry_l2
                            result[code] = {
                                "name": row.get("SECURITY_NAME_ABBR", ""),
                                "industry_l1": industry_l1,
                                "industry_l2": industry_l2,
                                "industry_l3": industry_l3,
                                "full": board_level,
                                "csrc": str(row.get("INDUSTRYCSRC1", "") or ""),
                            }
                        success = True
                        break
                    else:
                        logger.warning("datacenter 无数据，页 %d: %s", page, data.get("message", ""))
                        break
                else:
                    logger.warning("datacenter 状态码 %d，页 %d", r.status_code, page)
            except Exception as e:
                err = str(e)[:60]
                logger.warning("datacenter 异常，页 %d，重试 %d/%d: %s", page, attempt + 1, max_retries, err)
                time.sleep(1)

        if not success:
            logger.warning("页 %d 拉取失败，已有 %d 只，停止", page, len(result))
            break

        # 进度日志
        if page % 50 == 0 or page == 1:
            logger.info("拉取进度: 页 %d/%d, 已获取 %d/%d 只 (%.0f%%)",
                        page, total_pages, len(result), total_count,
                        len(result) / total_count * 100 if total_count else 0)

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.1)  # 轻微降频

    logger.info("全量 A 股行业映射: %d 只", len(result))
    return result


# ---------------------------------------------------------------------------
# 步骤 3: 合并排名 + 行业归属 → 个股板块分类
# ---------------------------------------------------------------------------

def build_stock_sector_classification(
    ranking: List[dict],
    stock_map: Dict[str, dict],
) -> Dict[str, dict]:
    """
    合并板块排名 + 个股行业归属 → 每只股票的板块分类。

    逻辑：
      1. 从 stock_map 取个股的 industry_l2（东财标准二级行业，如"半导体"）
      2. 在 ranking（东财 push2 行业板块涨跌幅）里精确匹配同名板块
      3. 继承该板块的 classification（主线/轮动/退潮）和 change_pct
      4. industry_l2 未命中时降级用 industry_l1（一级）

    东财 push2 板块名和 BOARD_NAME_LEVEL 的 l2 是同一套分类体系，精确匹配率 100%。
    不需要清洗罗马数字或模糊匹配（那是同花顺体系才需要的）。

    Returns:
        {stock_code: {
            "industry": "半导体",          # 最终匹配到的行业名（l2 优先）
            "industry_l1": "电子",         # 东财一级（备用）
            "industry_l2": "半导体",       # 东财二级（主用）
            "industry_l3": "半导体设备",    # 东财三级（备用）
            "classification": "main_trend",
            "change_pct": 3.2,
            "rank": 38,
            "match_type": "精确"/"部分"/"默认轮动",
        }}
    """
    # 建板块名 → 排名索引（东财 push2 板块名）
    name_to_rank = {b["name"]: b for b in ranking}

    result = {}
    exact_count = 0
    partial_count = 0
    default_count = 0

    for code, info in stock_map.items():
        industry_l1 = info.get("industry_l1", "")
        industry_l2 = info.get("industry_l2", "")
        industry_l3 = info.get("industry_l3", "")

        # 优先用二级精确匹配（东财 l2 和 push2 板块名同一体系）
        matched = None
        match_industry = ""
        match_type = ""

        if industry_l2 and industry_l2 in name_to_rank:
            matched = name_to_rank[industry_l2]
            match_industry = industry_l2
            match_type = "精确"
        elif industry_l1 and industry_l1 in name_to_rank:
            # 降级用一级
            matched = name_to_rank[industry_l1]
            match_industry = industry_l1
            match_type = "精确(降级一级)"
        else:
            # 部分匹配（l2 ⊂ 板块名 或 板块名 ⊂ l2）
            for name, r in name_to_rank.items():
                if industry_l2 and len(industry_l2) >= 2 and (industry_l2 in name or name in industry_l2):
                    matched = r
                    match_industry = industry_l2
                    match_type = "部分"
                    break
                if industry_l1 and len(industry_l1) >= 2 and (industry_l1 in name or name in industry_l1):
                    matched = r
                    match_industry = industry_l1
                    match_type = "部分(降级一级)"
                    break

        if matched:
            result[code] = {
                "industry": match_industry,
                "industry_l1": industry_l1,
                "industry_l2": industry_l2,
                "industry_l3": industry_l3,
                "full": info.get("full", ""),
                "classification": matched["classification"],
                "change_pct": matched["change_pct"],
                "rank": matched["rank"],
                "match_type": match_type,
            }
            if "精确" in match_type:
                exact_count += 1
            else:
                partial_count += 1
        else:
            # 默认轮动
            result[code] = {
                "industry": industry_l2 or industry_l1 or "未知",
                "industry_l1": industry_l1,
                "industry_l2": industry_l2,
                "industry_l3": industry_l3,
                "full": info.get("full", ""),
                "classification": "rotational",
                "change_pct": 0,
                "rank": 0,
                "match_type": "默认轮动(板块排名无此行业)",
            }
            default_count += 1

    logger.info("板块分类合并完成: %d 只, 精确=%d, 部分=%d, 默认轮动=%d",
                len(result), exact_count, partial_count, default_count)
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build(force: bool = False):
    """全量构建板块映射表"""
    cache_key_ranking = "sector_industry_ranking"
    cache_key_map = "stock_sector_classification"

    # 检查缓存
    if not force:
        cached_ranking = _cache_get(cache_key_ranking)
        cached_map = _cache_get(cache_key_map)
        if cached_ranking and cached_map:
            logger.info("缓存有效，跳过构建（--force 强制重建）")
            logger.info("  板块排名: %d 个", len(cached_ranking))
            logger.info("  个股映射: %d 只", len(cached_map))
            return

    logger.info("="*60)
    logger.info("开始构建板块映射表")
    logger.info("="*60)

    # 步骤 1: 东财行业板块涨跌幅排名
    logger.info("\n--- 步骤 1: 东财行业板块涨跌幅排名 ---")
    t0 = time.time()
    ranking = fetch_industry_board_ranking()
    logger.info("耗时: %.1fs, 板块数: %d", time.time() - t0, len(ranking))
    if not ranking:
        logger.error("板块排名拉取失败，终止")
        return
    _cache_set(cache_key_ranking, ranking, days=1)  # 排名每日更新，1 天有效期

    # 步骤 2: 全量 A 股行业归属
    logger.info("\n--- 步骤 2: 全量 A 股行业归属 ---")
    t0 = time.time()
    stock_map = fetch_all_stock_industry_map()
    logger.info("耗时: %.1fs, 个股数: %d", time.time() - t0, len(stock_map))
    if not stock_map:
        logger.error("个股行业映射拉取失败，终止")
        return

    # 步骤 3: 合并
    logger.info("\n--- 步骤 3: 合并排名 + 行业归属 ---")
    classification = build_stock_sector_classification(ranking, stock_map)

    # 保存
    _cache_set(cache_key_map, classification, days=30)  # 行业归属 30 天有效

    # 汇总
    logger.info("\n" + "="*60)
    logger.info("构建完成")
    logger.info("="*60)
    from collections import Counter
    cls_count = Counter(v["classification"] for v in classification.values())
    logger.info("分类统计: %s", dict(cls_count))

    # 打印测试股票
    test_codes = ["688009", "688027", "920045", "001399", "000001", "600519", "002594"]
    logger.info("\n测试股票:")
    for code in test_codes:
        info = classification.get(code, {})
        logger.info("  %s: %s (%s, 涨跌%.2f%%, %s)",
                    code, info.get("industry", "?"),
                    info.get("classification", "?"),
                    info.get("change_pct", 0),
                    info.get("match_type", "?"))


def check():
    """检查缓存状态"""
    logger.info("="*60)
    logger.info("缓存状态检查")
    logger.info("="*60)

    conn = _get_db_conn()
    cursor = conn.cursor()
    for key in ["sector_industry_ranking", "stock_sector_classification"]:
        cursor.execute(
            "SELECT cache_key, created_at, expire_at, length(cache_value) as size FROM data_cache WHERE cache_key = ?",
            (key,),
        )
        row = cursor.fetchone()
        if row:
            logger.info("  %s:", key)
            logger.info("    创建: %s", row["created_at"])
            logger.info("    过期: %s", row["expire_at"])
            logger.info("    大小: %d 字节", row["size"])
            # 检查是否过期
            cursor.execute("SELECT expire_at > datetime('now') as valid FROM data_cache WHERE cache_key = ?", (key,))
            valid = cursor.fetchone()["valid"]
            logger.info("    状态: %s", "✓ 有效" if valid else "✗ 已过期")
        else:
            logger.info("  %s: 未构建", key)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="板块映射表预构建")
    parser.add_argument("--force", action="store_true", help="强制重建（忽略缓存）")
    parser.add_argument("--check", action="store_true", help="仅检查缓存状态")
    args = parser.parse_args()

    if args.check:
        check()
    else:
        build(force=args.force)
