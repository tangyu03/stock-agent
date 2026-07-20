"""
基于市场涨跌幅的板块分类器

分类体系：
1. 同花顺行业（90个）— 用行业指数K线算涨跌幅
2. 新浪行业概念（~145个）— 真正的行业概念（光伏/锂矿/创新药等），用实时涨跌幅
3. 过滤掉风格标签（科创50/含H股/专精特新/次新股等）— 这些不是行业板块

板块分类：
- 前 20% = 主线（main_trend）
- 后 20% = 退潮（retreating）
- 中间 = 轮动（rotational）

个股打标：
- 一只股票可能在多个板块里，取最严格的标签
- 退潮 > 主线 > 轮动
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 风格标签关键词 — 这些不是行业板块，不应参与退潮判断
STYLE_KEYWORDS = [
    "ST", "含H股", "含B股", "含GDR", "科创50", "专精特新", "次新股",
    "融资融券", "三板", "超大盘", "送转", "重组", "央企", "国资",
    "自贸", "新区", "特区", "规划", "改革", "三角", "西岸",
    "朝鲜", "海南", "雄安", "黄河", "海峡", "成渝", "武汉", "皖江",
    "天津", "上海", "内贸", "水域", "油气改革", "金融改革", "国企",
    "准ST", "低价", "高价", "低市盈率", "高市盈率", "破净",
]


def _is_style_tag(name: str) -> bool:
    """判断是否为风格标签（非行业板块）"""
    for kw in STYLE_KEYWORDS:
        if kw in name:
            return True
    return False


def fetch_sector_rankings() -> List[dict]:
    """
    拉取行业板块涨跌幅排名

    数据源：
    1. 新浪行业（49 个）— 全部保留
    2. 新浪概念（175 个）— 过滤掉风格标签，只保留行业概念（~145 个）

    Returns:
        [
            {
                "type": "行业"/"概念",
                "name": 板块名,
                "label": 新浪标签,
                "change_pct": 涨跌幅%,
                "stock_count": 成分股数,
                "rank": 排名,
                "classification": "main_trend"/"rotational"/"retreating"
            },
            ...
        ]
        按涨跌幅降序排序
    """
    import akshare as ak

    all_sectors = []

    # 1. 新浪行业（49 个，过滤风格标签）
    try:
        df = ak.stock_sector_spot(indicator="新浪行业")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("板块", ""))
                if _is_style_tag(name):
                    continue  # 跳过风格标签
                all_sectors.append({
                    "type": "行业",
                    "name": name,
                    "label": str(row.get("label", "")),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "stock_count": int(row.get("公司家数", 0)),
                })
    except Exception as e:
        logger.warning("新浪行业板块拉取失败: %s", e)

    # 2. 新浪概念（175 个，过滤风格标签）
    try:
        df = ak.stock_sector_spot(indicator="概念")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("板块", ""))
                if _is_style_tag(name):
                    continue  # 跳过风格标签
                all_sectors.append({
                    "type": "概念",
                    "name": name,
                    "label": str(row.get("label", "")),
                    "change_pct": float(row.get("涨跌幅", 0)),
                    "stock_count": int(row.get("公司家数", 0)),
                })
    except Exception as e:
        logger.warning("新浪概念板块拉取失败: %s", e)

    # 按涨跌幅降序排序
    all_sectors.sort(key=lambda x: x["change_pct"], reverse=True)

    # 分类：前 20% 主线，后 20% 退潮，中间轮动
    n = len(all_sectors)
    if n == 0:
        return []

    top_n = max(1, n // 5)
    bottom_n = max(1, n // 5)

    for i, s in enumerate(all_sectors):
        s["rank"] = i + 1
        s["total"] = n
        if i < top_n:
            s["classification"] = "main_trend"
        elif i >= n - bottom_n:
            s["classification"] = "retreating"
        else:
            s["classification"] = "rotational"

    logger.info("板块排名: %d 个板块(行业+概念,已过滤风格标签), 前20%%=%d主线, 后20%%=%d退潮",
                n, top_n, bottom_n)

    return all_sectors


def build_stock_sector_index(
    stock_codes: List[str],
    rankings: List[dict],
) -> Dict[str, List[dict]]:
    """
    查持仓股在哪些板块里

    扫描全部板块（按涨跌幅从高到低），为每只股票收集其所属的**所有**板块信息。
    一只股票可能同时属于多个行业/概念，全部保留，由调用方按"退潮 > 主线 > 轮动"
    的优先级选取最严格标签。

    注意：之前的实现存在两个 Bug 已修复：
      1. 缺少 `return result`，导致返回 None，调用方 .get() 报错
         "'NoneType' object has no attribute 'get'"
      2. 匹配后 `target_set.discard(code)` 提前移除，导致每只股票只记录一个板块，
         板块数据抓取不全。现已移除，扫描全部板块以收集完整板块归属。

    Args:
        stock_codes: 持仓股代码列表
        rankings: fetch_sector_rankings() 返回的板块列表

    Returns:
        {stock_code: [板块信息, ...]}，每只股票可能对应多个板块
    """
    import akshare as ak

    target_set = set(stock_codes)
    result: Dict[str, List[dict]] = {code: [] for code in stock_codes}
    scanned = 0
    sample_codes = None  # 记录第一个板块的代码样本用于诊断
    failed_sectors = 0   # detail API 失败计数

    for sector in rankings:
        # 不再提前 break：需要扫描全部板块才能收集每只股票的所有归属
        label = sector.get("label", "")
        if not label:
            continue
        try:
            df = ak.stock_sector_detail(sector=label)
            if df is not None and not df.empty:
                code_col = "code" if "code" in df.columns else df.columns[1]
                from ..data_layer.sw_industry import _extract_code
                sector_codes = set(_extract_code(r[code_col]) for _, r in df.iterrows())
                # 诊断：记录第一个板块的代码样本
                if sample_codes is None and sector_codes:
                    sample_codes = list(sector_codes)[:5]
                    logger.debug("板块 '%s' 代码样本(提取后): %s, 原始列名: %s",
                                 sector["name"], sample_codes, code_col)
                # 计算交集（不 discard，保留 target_set 以收集所有板块归属）
                matched = target_set & sector_codes
                for code in matched:
                    result[code].append({
                        "type": sector["type"],
                        "name": sector["name"],
                        "change_pct": sector["change_pct"],
                        "classification": sector["classification"],
                        "rank": sector["rank"],
                    })
                scanned += 1
            else:
                failed_sectors += 1
        except Exception as e:
            failed_sectors += 1
            if failed_sectors <= 3:
                logger.debug("板块 '%s' detail API 失败: %s", sector.get("name", label), str(e)[:80])

    # 统计：命中至少一个板块的股票数 + 平均每只命中的板块数
    hit_stocks = sum(1 for v in result.values() if v)
    total_hits = sum(len(v) for v in result.values())
    avg_sectors = (total_hits / hit_stocks) if hit_stocks else 0
    unmatched = [c for c, v in result.items() if not v]

    logger.info(
        "板块成分股查询完成: 扫描 %d 个板块(失败 %d), 命中 %d/%d 只, "
        "板块归属总数 %d (平均 %.1f 个/股), 未命中 %d 只%s",
        scanned, failed_sectors, hit_stocks, len(stock_codes),
        total_hits, avg_sectors, len(unmatched),
        f": {','.join(sorted(unmatched))}" if unmatched else "",
    )
    if sample_codes:
        logger.info("板块代码样本(提取后): %s, 目标代码样本: %s",
                    sample_codes, sorted(stock_codes)[:5])

    return result

def classify_stocks(
    stock_codes: List[str],
) -> Dict[str, dict]:
    """
    给持仓股打板块标签

    一只股票可能在多个板块里，取最严格的标签：
    - 如果在任何一个"退潮"板块里 → 退潮
    - 如果在任何一个"主线"板块里 → 主线
    - 否则 → 轮动

    Args:
        stock_codes: 股票代码列表

    Returns:
        {stock_code: {
            "classification": "main_trend"/"rotational"/"retreating",
            "sectors": [该股所属的所有板块],
            "best_sector": 最严格的板块,
        }}
    """
    # 0. 优先查预构建的映射表（scripts/build_sector_mapping.py 构建，30 天有效）
    # 这是机构级数据源：东财行业板块涨跌幅排名 + 东财三级行业归属
    # 不反爬，盘中直接查表，0 API 调用
    table_result = _lookup_from_cache_table(stock_codes)
    if table_result:
        hit_count = sum(1 for v in table_result.values() if v.get("classification") != "unknown")
        if hit_count == len(stock_codes):
            logger.info("sector_ranker: 全部 %d 只命中预构建映射表，0 API 调用", len(stock_codes))
            return table_result
        # 部分命中，对未命中的继续走实时 API
        missing_codes = [c for c in stock_codes if c not in table_result or table_result[c].get("classification") == "unknown"]
        logger.info("sector_ranker: 映射表命中 %d/%d，剩余 %d 只走实时 API",
                    hit_count, len(stock_codes), len(missing_codes))
        # 对未命中的走实时 API
        realtime_result = _classify_stocks_realtime(missing_codes)
        # 合并
        for code, info in realtime_result.items():
            table_result[code] = info
        return table_result

    # 映射表未构建或读取失败，走实时 API
    return _classify_stocks_realtime(stock_codes)


def _lookup_from_cache_table(stock_codes: List[str]) -> Optional[Dict[str, dict]]:
    """
    从预构建的映射表查个股板块分类（0 API 调用）。

    数据来源：scripts/build_sector_mapping.py 构建的 stock_sector_classification 缓存
    数据源：东财行业板块涨跌幅（push2）+ 东财三级行业归属（datacenter）

    盘中实时刷新涨跌幅：
      个股归属表 30 天有效（行业归属几乎不变）
      板块涨跌幅每日变化 → 调用 _refresh_daily_ranking() 实时拉取当日涨跌幅
      合并：表里的行业归属 × 当日实时涨跌幅 → 最终分类

    Returns:
        {code: {classification, sectors, best_sector}} 或 None（表未构建）
    """
    try:
        import json
        from ..db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value FROM data_cache WHERE cache_key = ? AND expire_at > datetime('now')",
            ("stock_sector_classification",),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None

        classification = json.loads(row["cache_value"])

        # 实时刷新当日板块涨跌幅（盘中调用，带当日缓存）
        # 个股归属表 30 天有效，但板块涨跌幅每日变化，需要实时拉
        daily_ranking = _refresh_daily_ranking()

        result = {}
        for code in stock_codes:
            info = classification.get(code)
            if info:
                # 优先用当日实时涨跌幅，降级用表里的（表里的涨跌幅是构建时的）
                industry = info.get("industry", "")
                change_pct = info.get("change_pct", 0)
                cls = info.get("classification", "rotational")
                rank = info.get("rank", 0)

                if daily_ranking:
                    # 东财 push2 板块名和 industry_l2 同一体系，直接精确匹配
                    daily_match = daily_ranking.get(industry)
                    if not daily_match:
                        # 部分匹配（l2 ⊂ 板块名 或 板块名 ⊂ l2）
                        for name, r in daily_ranking.items():
                            if len(industry) >= 2 and (industry in name or name in industry):
                                daily_match = r
                                break
                    if daily_match:
                        change_pct = daily_match.get("change_pct", change_pct)
                        cls = daily_match.get("classification", cls)
                        rank = daily_match.get("rank", rank)

                sector_entry = {
                    "type": "东财行业",
                    "name": industry,
                    "change_pct": change_pct,
                    "classification": cls,
                    "rank": rank,
                }
                result[code] = {
                    "classification": cls,
                    "sectors": [sector_entry],
                    "best_sector": sector_entry,
                }
            else:
                result[code] = {"classification": "unknown", "sectors": [], "best_sector": None}
        return result
    except Exception as e:
        logger.debug("查映射表失败: %s", e)
        return None


# 当日板块涨跌幅缓存（进程内 + SQLite 当日缓存）
_daily_ranking_cache: Optional[Dict[str, dict]] = None
_daily_ranking_cache_date: str = ""


def _refresh_daily_ranking() -> Optional[Dict[str, dict]]:
    """
    实时拉取当日东财行业板块涨跌幅（盘中调用）。

    数据源：东财 push2 clist（fs=m:90+t:2 行业板块，496 个标准二级行业）
    带重试机制（push2 偶发反爬，成功率约 80%，10 次重试足够）。

    为什么用东财 push2？
    - push2 板块名和个股 BOARD_NAME_LEVEL 的 industry_l2 是同一套分类体系，精确匹配 100%
    - 同花顺是另一套体系（90 个行业），和东财 l2 不一致，会导致降级匹配
    - push2 虽偶发反爬但带重试可用，且是当日实时涨跌幅（比同花顺 3 日更准）

    缓存策略：
      - 进程内缓存：当日有效
      - SQLite 缓存：当日有效（跨进程复用）

    Returns:
        {板块名: {"change_pct": float, "classification": str, "rank": int}}
        拉取失败返回 None（降级用映射表里的旧涨跌幅）
    """
    global _daily_ranking_cache, _daily_ranking_cache_date
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 进程内缓存
    if _daily_ranking_cache is not None and _daily_ranking_cache_date == today:
        return _daily_ranking_cache

    # 2. SQLite 当日缓存
    try:
        import json
        from ..db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value FROM data_cache WHERE cache_key = ? AND expire_at > datetime('now')",
            ("daily_industry_ranking",),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            _daily_ranking_cache = json.loads(row["cache_value"])
            _daily_ranking_cache_date = today
            logger.info("板块涨跌幅命中 SQLite 当日缓存")
            return _daily_ranking_cache
    except Exception:
        pass

    # 3. 实时拉取（东财 push2，带重试）
    try:
        import requests
        import time as _time
        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0"
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        all_boards = []
        page = 1
        max_pages = 6  # 496 个 / 100 = 5 页

        while page <= max_pages:
            params = {
                "pn": str(page), "pz": "100", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fields": "f12,f14,f3",
                "fs": "m:90+t:2",  # 行业板块（标准东财二级行业）
                "ut": "f0ce0975da3f5d44e7b8e8b2e8a8a8a8",
            }
            success = False
            for attempt in range(10):  # 10 次重试
                try:
                    r = requests.get(url, params=params, timeout=12,
                                     headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("data") and data["data"].get("diff"):
                            diff = data["data"]["diff"]
                            for b in diff:
                                all_boards.append({
                                    "name": b.get("f14", ""),
                                    "change_pct": float(b.get("f3", 0)),
                                })
                            total = data["data"].get("total", 0)
                            success = True
                            break
                    _time.sleep(3)
                except Exception:
                    _time.sleep(3)

            if not success:
                # 当前页失败，继续下一页（不中断，已获取的部分仍有用）
                logger.warning("板块涨跌幅页 %d 失败，继续下一页，已获取 %d 个",
                               page, len(all_boards))
                page += 1
                _time.sleep(2)
                continue

            if len(all_boards) >= total or len(diff) < 100:
                break
            page += 1
            _time.sleep(1.5)

        if not all_boards:
            return None

        if len(all_boards) < 400:
            logger.warning("板块涨跌幅部分失败：仅 %d/496 个（分类精度可能降低）", len(all_boards))

        # 排序 + 分类
        all_boards.sort(key=lambda x: x["change_pct"], reverse=True)
        n = len(all_boards)
        top_n = max(1, n // 5)
        bottom_n = max(1, n // 5)
        result = {}
        for i, b in enumerate(all_boards):
            if i < top_n:
                b["classification"] = "main_trend"
            elif i >= n - bottom_n:
                b["classification"] = "retreating"
            else:
                b["classification"] = "rotational"
            b["rank"] = i + 1
            result[b["name"]] = b

        # 写缓存
        _daily_ranking_cache = result
        _daily_ranking_cache_date = today
        try:
            import json
            from datetime import timedelta
            from ..db import get_connection
            conn = get_connection()
            cursor = conn.cursor()
            expiry = (datetime.now().replace(hour=23, minute=59, second=59) + timedelta(days=1)).isoformat()
            cursor.execute(
                "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) VALUES (?, ?, ?)",
                ("daily_industry_ranking", json.dumps(result, ensure_ascii=False), expiry),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        logger.info("板块涨跌幅实时拉取(东财push2): %d 个板块, 前20%%=%d主线, 后20%%=%d退潮",
                    n, top_n, bottom_n)
        return result
    except Exception as e:
        logger.warning("板块涨跌幅实时拉取失败: %s", str(e)[:80])
        return None


def _classify_stocks_realtime(stock_codes: List[str]) -> Dict[str, dict]:
    """
    实时 API 拉板块分类（映射表未命中时的降级路径）。

    数据源：新浪板块排名 + 新浪成分股 + 同花顺主营兜底
    """
    # 1. 拉板块排名
    rankings = fetch_sector_rankings()
    if not rankings:
        return {code: {"classification": "rotational", "sectors": []} for code in stock_codes}

    # 2. 查持仓股在哪些板块里
    stock_sectors = build_stock_sector_index(stock_codes, rankings)

    # 3. 给个股打标签
    # 先建 name→ranking 索引用于兜底反查
    name_to_rank = {}
    for r in rankings:
        n = r.get("name", "")
        if n:
            name_to_rank[n] = r

    result = {}
    found_count = 0
    fallback_count = 0
    default_count = 0  # 所有 API 失败，默认归入轮动的股票数

    import traceback

    for code in stock_codes:
        sectors = stock_sectors.get(code, [])

        if sectors:
            try:
                found_count += 1
                # 找最严格的标签：退潮 > 主线 > 轮动
                best = None
                for s in sectors:
                    if not isinstance(s, dict):
                        continue
                    if s.get("classification") == "retreating":
                        best = s
                        break
                    elif s.get("classification") == "main_trend" and best is None:
                        best = s

                if best is None:
                    best = sectors[0]

                result[code] = {
                    "classification": best.get("classification", "rotational"),
                    "sectors": sectors,
                    "best_sector": best,
                }
            except Exception as e:
                logger.warning("sector_ranker 命中股处理失败 %s: %s\n%s", code, e, traceback.format_exc())
                # 异常时也默认轮动，不标 unknown
                result[code] = {
                    "classification": "rotational",
                    "sectors": sectors,
                    "best_sector": sectors[0] if sectors else None,
                }
        else:
            # 兜底：新浪板块成分股不覆盖 → 用 SW 行业反查
            try:
                fallback_sectors = _fallback_sector_lookup(code, name_to_rank)
                if fallback_sectors:
                    fallback_count += 1
                    best = fallback_sectors[0]
                    if not isinstance(best, dict):
                        best = {"classification": "rotational"}
                    result[code] = {
                        "classification": best.get("classification", "rotational"),
                        "sectors": fallback_sectors,
                        "best_sector": best,
                    }
                else:
                    # 最终兜底：所有 API 都失败时，默认归入"轮动"（中间值）
                    # 不标 unknown — 前20%主线、后20%退潮、中间60%轮动
                    # 未知股票归入轮动是最安全的选择（不激进也不恐慌）
                    default_count += 1
                    result[code] = {
                        "classification": "rotational",
                        "sectors": [{
                            "type": "默认",
                            "name": "轮动(数据不足)",
                            "change_pct": 0,
                            "classification": "rotational",
                            "rank": 0,
                        }],
                        "best_sector": {
                            "type": "默认",
                            "name": "轮动(数据不足)",
                            "change_pct": 0,
                            "classification": "rotational",
                            "rank": 0,
                        },
                    }
            except Exception as e:
                logger.warning("sector_ranker 兜底处理失败 %s: %s\n%s", code, e, traceback.format_exc())
                # 异常时也默认轮动
                result[code] = {
                    "classification": "rotational",
                    "sectors": [{
                        "type": "默认",
                        "name": "轮动(数据不足)",
                        "change_pct": 0,
                        "classification": "rotational",
                        "rank": 0,
                    }],
                    "best_sector": None,
                }

    # 诊断
    sample_detail = []
    for code in stock_codes[:5]:
        rd = result.get(code, {})
        if rd:
            secs = rd.get("sectors", [])
            sec_names = [s["name"] for s in secs[:3] if isinstance(s, dict)]
            sample_detail.append(f"{code}({rd.get('classification','?')},{len(secs)}个:{sec_names})")
    logger.info("sector_ranker 分类结果: %d 只直接命中, %d 只兜底反查, %d 只默认轮动, 样本: %s",
                found_count, fallback_count, default_count,
                " | ".join(sample_detail))

    return result


# ---------------------------------------------------------------------------
# 同花顺主营介绍 → 关键词匹配行业（绕过东财反爬的稳定数据源）
# ---------------------------------------------------------------------------

# 同花顺行业列表缓存（90 个行业，当日有效）
_ths_industry_list_cache: Optional[List[str]] = None
_ths_industry_list_date: str = ""

# 同花顺主营介绍缓存（session 级，避免重复调用）
_ths_zyjs_cache: Dict[str, Dict] = {}

# 行业别名/关键词映射（主营关键词 → 同花顺行业名）
# 用于处理同花顺行业列表中没有直接对应的情况
_INDUSTRY_ALIAS = {
    "轨道交通": "运输设备",
    "铁路": "运输设备",
    "地铁": "运输设备",
    "量子": "通信设备",
    "光通信": "通信设备",
    "光纤": "通信设备",
    "光器件": "通信设备",
    "X射线": "通用设备",
    "检测设备": "通用设备",
    "智能检测": "通用设备",
    "新能源": "电池",
    "光伏": "光伏设备",
    "锂电": "电池",
    "储能": "电池",
    "芯片": "半导体",
    "集成电路": "半导体",
    "封测": "半导体",
    "AI": "软件开发",
    "人工智能": "软件开发",
    "大模型": "软件开发",
    "军工": "航空装备",
    "航天": "航天装备",
    "航空": "航空装备",
    "医药": "化学制药",
    "创新药": "化学制药",
    "医疗器械": "医疗器械",
    "中药": "中药",
    "白酒": "白酒",
    "食品": "食品加工",
    "银行": "银行",
    "证券": "证券",
    "保险": "保险",
    "房地产": "房地产开发",
    "钢铁": "普钢",
    "有色": "工业金属",
    "黄金": "贵金属",
    "煤炭": "煤炭开采",
    "石油": "油气开采",
    "化工": "化学制品",
    "机械": "通用设备",
    "机器人": "自动化设备",
    "汽车": "乘用车",
    "汽车零部件": "汽车零部件",
    "电力": "电力",
    "环保": "环境治理",
    "游戏": "游戏",
    "传媒": "数字媒体",
    "旅游": "酒店餐饮",
    "农业": "种植业",
    "纺织": "服装家纺",
    "建材": "水泥",
    "建筑": "房屋建设",
}


def _get_ths_industry_list() -> List[str]:
    """获取同花顺行业列表（当日缓存）"""
    global _ths_industry_list_cache, _ths_industry_list_date
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    if _ths_industry_list_cache and _ths_industry_list_date == today:
        return _ths_industry_list_cache

    try:
        import akshare as ak
        df = ak.stock_board_industry_name_ths()
        if df is not None and not df.empty and "name" in df.columns:
            _ths_industry_list_cache = df["name"].tolist()
            _ths_industry_list_date = today
            logger.info("同花顺行业列表已缓存: %d 个行业", len(_ths_industry_list_cache))
            return _ths_industry_list_cache
    except Exception as e:
        logger.debug("同花顺行业列表拉取失败: %s", e)
    return []


def _match_industry_by_keyword(business: str, product_type: str, industry_list: List[str]) -> Optional[str]:
    """
    用主营业务/产品类型关键词匹配同花顺行业。

    匹配优先级：
      1. 别名表匹配（INDUSTRY_ALIAS）
      2. 精确匹配（行业名完整出现在主营/产品里）
      3. 行业名前3字在主营里

    Returns:
        匹配到的行业名，未匹配返回 None
    """
    text = f"{business} {product_type}"

    # 1. 别名表匹配（最优先，因为别名表是人工校准的）
    for keyword, industry in _INDUSTRY_ALIAS.items():
        if keyword in text and industry in industry_list:
            return industry

    # 2. 精确匹配（行业名完整出现）
    for ind in industry_list:
        if ind in text:
            return ind

    # 3. 行业名前3字在主营里（降低误匹配）
    for ind in industry_list:
        if len(ind) >= 3 and ind[:3] in text:
            return ind

    return None


def _fetch_industry_from_ths(code: str) -> Optional[str]:
    """
    用同花顺主营介绍接口查个股行业归属（绕过东财反爬）。

    数据源：ak.stock_zyjs_ths(symbol=code) — 同花顺主营介绍（稳定，0.5s/只）
    逻辑：拉主营介绍 → 关键词匹配同花顺 90 个行业 → 返回行业名

    缓存：session 级缓存，同一次运行内不重复调 API。

    Returns:
        行业名（如"半导体"），失败返回 None
    """
    if code in _ths_zyjs_cache:
        return _ths_zyjs_cache[code].get("industry")

    try:
        import akshare as ak
        df = ak.stock_zyjs_ths(symbol=code)
        if df is None or df.empty:
            _ths_zyjs_cache[code] = {"industry": None}
            return None

        row = df.iloc[0]
        business = str(row.get("主营业务", ""))
        product_type = str(row.get("产品类型", ""))

        industry_list = _get_ths_industry_list()
        if not industry_list:
            _ths_zyjs_cache[code] = {"industry": None}
            return None

        matched = _match_industry_by_keyword(business, product_type, industry_list)
        _ths_zyjs_cache[code] = {
            "industry": matched,
            "business": business[:60],
            "product_type": product_type[:60],
        }

        if matched:
            logger.info("同花顺主营匹配 %s: '%s...' → 行业 '%s'", code, business[:20], matched)
        else:
            logger.debug("同花顺主营匹配 %s: 未匹配, 主营='%s...'", code, business[:30])

        return matched
    except Exception as e:
        logger.debug("同花顺主营查询失败 %s: %s", code, str(e)[:80])
        _ths_zyjs_cache[code] = {"industry": None}
        return None


# 同花顺行业指数 K 线缓存（当日有效）
_ths_industry_kline_cache: Dict[str, List[Dict]] = {}
_ths_industry_kline_date: str = ""


def _classify_by_ths_industry_kline(code: str, industry_name: str) -> List[dict]:
    """
    用同花顺行业指数 K 线算涨跌幅，直接给个股分类。

    当同花顺行业名不在新浪板块排名里时，用这个方法兜底。
    拉同花顺行业指数 K 线 → 算 3 日涨跌幅 → 用绝对值判断分类：
      - 涨幅 > 3% → main_trend（主线）
      - 跌幅 > 3% → retreating（退潮）
      - 其他 → rotational（轮动）

    数据源：ak.stock_board_industry_index_ths(symbol=行业名) — 同花顺行业指数 K 线（稳定）

    Returns:
        [板块条目] 或 []（K 线拉取失败时）
    """
    global _ths_industry_kline_cache, _ths_industry_kline_date
    from datetime import datetime, timedelta

    today = datetime.now().strftime("%Y-%m-%d")
    if _ths_industry_kline_date != today:
        _ths_industry_kline_cache = {}
        _ths_industry_kline_date = today

    # 检查缓存
    if industry_name in _ths_industry_kline_cache:
        kline = _ths_industry_kline_cache[industry_name]
    else:
        try:
            import akshare as ak
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            end_date = datetime.now().strftime("%Y%m%d")
            df = ak.stock_board_industry_index_ths(
                symbol=industry_name,
                start_date=start_date,
                end_date=end_date,
            )
            if df is None or df.empty:
                _ths_industry_kline_cache[industry_name] = []
                return []
            # 提取收盘价（akshare 同花顺 K 线列名是"收盘价"）
            close_col = "收盘价" if "收盘价" in df.columns else (
                "收盘" if "收盘" in df.columns else (
                    "close" if "close" in df.columns else None
                )
            )
            if not close_col:
                _ths_industry_kline_cache[industry_name] = []
                return []
            kline = [float(df.iloc[i][close_col]) for i in range(len(df))]
            _ths_industry_kline_cache[industry_name] = kline
        except Exception as e:
            logger.debug("同花顺行业 K 线拉取失败 '%s': %s", industry_name, str(e)[:60])
            _ths_industry_kline_cache[industry_name] = []
            return []

    if not kline or len(kline) < 4:
        return []

    # 算 3 日涨跌幅
    latest = kline[-1]
    three_days_ago = kline[-4]
    if three_days_ago <= 0:
        return []
    change_3d = (latest - three_days_ago) / three_days_ago  # 小数

    # 用绝对值判断分类
    if change_3d > 0.03:  # 涨幅 > 3%
        classification = "main_trend"
    elif change_3d < -0.03:  # 跌幅 > 3%
        classification = "retreating"
    else:
        classification = "rotational"

    logger.info(
        "同花顺行业 K 线分类 %s: 行业 '%s' 3日涨跌=%.2f%% → %s",
        code, industry_name, change_3d * 100, classification,
    )

    return [{
        "type": "同花顺行业",
        "name": industry_name,
        "change_pct": round(change_3d * 100, 2),
        "classification": classification,
        "rank": 0,
    }]


def _fallback_sector_lookup(code: str, name_to_rank: Dict[str, dict]) -> List[dict]:
    """
    兜底：对未被新浪板块成分股覆盖的股票，通过多个数据源反查排名。

    科创板(688xxx)/北交所(920xxx/83xxxx/87xxxx/92xxxx)和部分创业板(300xxx/301xxx)
    在新浪板块成分股列表中缺失，需要用其他数据源补齐。

    多策略匹配（按优先级）：
      0. **同花顺主营匹配**（最优先，稳定不反爬）：
         用 stock_zyjs_ths 拉主营介绍 → 关键词匹配同花顺行业 → 查新浪排名
      1. SW 行业精确匹配：SW 行业名 == 新浪板块名
      2. SW 行业部分匹配：SW 行业名 ⊂ 新浪板块名
      3. SW 二级代码直接计算板块 K 线指标

    数据源稳定性：
      - 同花顺 stock_zyjs_ths: ✅ 稳定（0.5s/只，不反爬）
      - 东财 stock_individual_info_em: ❌ 持续反爬
      - SW index_component_sw: ❌ 持续反爬
    """
    # --- 策略 0: 同花顺主营匹配（最优先，绕过东财反爬）---
    try:
        ths_industry = _fetch_industry_from_ths(code)
        if ths_industry:
            # 用同花顺行业名查新浪板块排名
            if ths_industry in name_to_rank:
                r = name_to_rank[ths_industry]
                if isinstance(r, dict):
                    logger.info("sector_ranker 兜底: %s 同花顺行业 '%s' 精确匹配新浪排名", code, ths_industry)
                    return [_build_fallback_entry(ths_industry, r, "同花顺主营-精确")]
            # 部分匹配
            for name, r in name_to_rank.items():
                if not isinstance(r, dict):
                    continue
                if len(ths_industry) >= 2 and (ths_industry in name or name in ths_industry):
                    logger.info("sector_ranker 兜底: %s 同花顺行业 '%s' 部分匹配 '%s'", code, ths_industry, name)
                    return [_build_fallback_entry(ths_industry, r, "同花顺主营-部分")]

            # 新浪排名里没找到同花顺行业名 → 直接拉同花顺行业指数 K 线算涨跌幅
            # 用涨跌幅绝对值判断分类：>3% 主线，<-3% 退潮，中间轮动
            ths_result = _classify_by_ths_industry_kline(code, ths_industry)
            if ths_result:
                return ths_result
    except Exception as e:
        logger.debug("sector_ranker 兜底: %s 同花顺主营匹配异常: %s", code, str(e)[:60])

    # --- 策略 1-3: THS 行业匹配 ---
    try:
        from ..data_layer.sw_industry import (
            fetch_stock_sw_industry_full,
            normalize_sector,
            calc_sector_metrics,
        )

        full = fetch_stock_sw_industry_full(code)
        if full is None:
            logger.debug("sector_ranker 兜底: %s 行业查询返回 None", code)
            return []

        level2 = full.get("level2")
        level1 = full.get("level1")
        ths_industry_name = level2 or level1
        if not ths_industry_name:
            logger.debug("sector_ranker 兜底: %s 行业(level1+level2) 均为空, full=%s", code, full)
            return []

        # --- 策略 1: 精确匹配 ---
        if ths_industry_name in name_to_rank:
            r = name_to_rank[ths_industry_name]
            if isinstance(r, dict):
                logger.debug("sector_ranker 兜底: %s THS行业 '%s' 精确匹配命中", code, ths_industry_name)
                return [_build_fallback_entry(ths_industry_name, r, "THS行业-精确")]

        # --- 策略 2: 部分匹配 ---
        for name, r in name_to_rank.items():
            if not isinstance(r, dict):
                continue
            if len(ths_industry_name) >= 2 and (ths_industry_name in name or name in ths_industry_name):
                logger.debug("sector_ranker 兜底: %s THS行业 '%s' 部分匹配 '%s'", code, ths_industry_name, name)
                return [_build_fallback_entry(ths_industry_name, r, "THS行业-部分")]

        # --- 策略 3: 用 THS 代码直接计算板块 K 线指标 ---
        ths_code = normalize_sector(ths_industry_name)
        if ths_code:
            logger.debug(
                "sector_ranker 兜底: %s THS行业 '%s' → 代码 %s，直接计算 K 线指标",
                code, ths_industry_name, ths_code,
            )
            return _compute_sw_fallback(code, ths_code, ths_industry_name)

        # 所有策略均失败
        logger.debug(
            "sector_ranker 兜底: %s THS行业 '%s' 所有匹配策略均失败 (level1=%s, level2=%s)",
            code, ths_industry_name, level1, level2,
        )
        return []
    except Exception:
        import traceback
        logger.debug("sector_ranker 兜底 %s 异常: %s", code, traceback.format_exc())
        return []


def _build_fallback_entry(name: str, rank_info: dict, source: str = "THS行业") -> dict:
    """构造兜底板块条目"""
    return {
        "type": source,
        "name": name,
        "change_pct": rank_info.get("change_pct", 0),
        "classification": rank_info.get("classification", "rotational"),
        "rank": rank_info.get("rank", 0),
    }


def _compute_sw_fallback(code: str, ths_code: str, ths_name: str) -> List[dict]:
    """
    最后兜底：直接计算 THS 板块 K 线指标，自行分类。

    当新浪板块名无法匹配 THS 行业名时，
    用 calc_sector_metrics 拉取 THS 行业指数 K 线，根据涨跌幅 + 均线排列
    给出 main_trend/rotational/retreating 分类。

    Returns:
        [板块条目] 或 []（K线拉取失败时）
    """
    try:
        from ..data_layer.sw_industry import calc_sector_metrics

        metrics = calc_sector_metrics(ths_code)
        if not metrics:
            logger.debug("sector_ranker 兜底计算: %s THS板块 %s 指标为空", code, ths_code)
            return []

        # 3 日涨跌幅（已是百分比）
        change_3d = metrics.get("sector_change_3d")
        change_5d = metrics.get("sector_change_5d")
        change_pct = float(change_3d if change_3d is not None else (change_5d if change_5d is not None else 0))
        # change_3d/change_5d 已是百分比（calc_sector_metrics 返回 *100 后的值）

        # 均线排列 + MA20 位置 → 分类
        ma_align = metrics.get("ma_alignment", "cross")
        above_ma20 = metrics.get("sector_above_ma20", False)
        if ma_align == "bullish" and above_ma20:
            classification = "main_trend"
        elif ma_align == "bearish" and not above_ma20:
            classification = "retreating"
        else:
            classification = "rotational"

        logger.debug(
            "sector_ranker 兜底计算: %s THS板块 %s(%s) change=%.2f%% align=%s ma20=%s → %s",
            code, ths_name, ths_code, change_pct, ma_align, above_ma20, classification,
        )
        return [{
            "type": "THS行业-计算",
            "name": ths_name,
            "change_pct": change_pct,
            "classification": classification,
            "rank": 0,
        }]
    except Exception as e:
        logger.debug("sector_ranker 兜底计算 %s THS板块 %s 异常: %s", code, ths_code, e)
        return []



def print_rankings(rankings: List[dict], top_n: int = 10):
    """打印板块排名"""
    print()
    print("=" * 70)
    print(f"  板块涨跌幅排名（共 {len(rankings)} 个：行业+概念，已过滤风格标签）")
    print("=" * 70)
    print(f"  {'排名':<4} {'类型':<4} {'板块':<14} {'涨跌幅%':>8} {'分类':<8}")
    print("  " + "-" * 50)

    for s in rankings[:top_n]:
        cls = {"main_trend": "主线", "rotational": "轮动", "retreating": "退潮"}.get(
            s["classification"], ""
        )
        print(f"  {s['rank']:<4} {s['type']:<4} {s['name']:<14} {s['change_pct']:>+8.2f} {cls:<8}")

    if len(rankings) > top_n:
        retreating = [s for s in rankings if s["classification"] == "retreating"]
        if retreating:
            print(f"\n  退潮板块（后 20%）:")
            for s in retreating[:10]:
                print(f"  {s['rank']:<4} {s['type']:<4} {s['name']:<14} {s['change_pct']:>+8.2f} 退潮")

    print("=" * 70)
