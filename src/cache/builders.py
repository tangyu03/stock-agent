"""
Step1 板块层面数据拉取纯逻辑（无 DB 读写、可独立单测）

1. fetch_eastmoney_ranking — 东财 push2 行业板块涨跌幅排名（抽自 sector_ranker._refresh_daily_ranking 的请求逻辑）
2. fetch_components_concurrent — 并发拉各板块成分股
3. compute_sector_metrics_from_kline — 从 K 线算轻量指标（优先 ths_cache 历史，零 API）
4. classify_sector_status / classify_by_percentile — 分类策略

注意：本模块不做 DB 写入；worker 线程只拉数据回传，指标计算/落库在主线程。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0"


# ===========================================================================
# 板块排名（东财 push2）
# ===========================================================================

def fetch_eastmoney_ranking(retries: int = 10, max_pages: int = 6) -> Optional[Dict[str, dict]]:
    """
    实时拉取东财行业板块涨跌幅排名（496 个标准二级行业）。

    数据源：东财 push2 clist（fs=m:90+t:2），带重试（push2 偶发反爬，成功率约 80%）。
    分类：前 20% 主线 / 后 20% 退潮 / 中间轮动（与 sector_ranker 口径一致）。

    Returns:
        {板块名: {"change_pct": float, "classification": str, "rank": int}}
        拉取失败返回 None
    """
    import requests

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    all_boards: List[dict] = []
    page = 1

    while page <= max_pages:
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fields": "f12,f14,f3",
            "fs": "m:90+t:2",  # 行业板块（标准东财二级行业）
            "ut": "f0ce0975da3f5d44e7b8e8b2e8a8a8a8",
        }
        success = False
        diff = []
        for attempt in range(retries):
            try:
                r = requests.get(url, params=params, timeout=12,
                                 headers={"User-Agent": UA,
                                          "Referer": "https://quote.eastmoney.com/"})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("data") and data["data"].get("diff"):
                        diff = data["data"]["diff"]
                        success = True
                        break
                time.sleep(3)
            except Exception:
                time.sleep(3)
        if not success:
            logger.warning("板块涨跌幅页 %d 失败，继续下一页，已获取 %d 个",
                           page, len(all_boards))
            page += 1
            time.sleep(2)
            continue

        for b in diff:
            all_boards.append({
                "name": b.get("f14", ""),
                "change_pct": float(b.get("f3", 0)),
            })

        total = 0
        try:
            total = int(diff and len(diff) or 0)
        except Exception:
            total = 0
        if len(all_boards) >= total or len(diff) < 100:
            break
        page += 1
        time.sleep(1.5)

    if not all_boards:
        return None
    if len(all_boards) < 400:
        logger.warning("板块涨跌幅部分失败：仅 %d/496 个（分类精度可能降低）", len(all_boards))

    all_boards.sort(key=lambda x: x["change_pct"], reverse=True)
    n = len(all_boards)
    top_n = max(1, n // 5)
    bottom_n = max(1, n // 5)
    result: Dict[str, dict] = {}
    for i, b in enumerate(all_boards):
        if i < top_n:
            b["classification"] = "main_trend"
        elif i >= n - bottom_n:
            b["classification"] = "retreating"
        else:
            b["classification"] = "rotational"
        b["rank"] = i + 1
        result[b["name"]] = b

    logger.info("板块涨跌幅实时拉取(东财push2): %d 个板块, 前20%%=%d主线, 后20%%=%d退潮",
                n, top_n, bottom_n)
    return result


def classify_by_percentile(ranking: Dict[str, dict]) -> Dict[str, str]:
    """百分位分类：前 20% 主线 / 后 20% 退潮 / 中间轮动（与排名内置分类一致）"""
    result = {}
    for name, info in ranking.items():
        result[name] = info.get("classification", "rotational")
    return result


# ===========================================================================
# 全量 A 股行业归属（datacenter，不反爬）— 复用 scripts/build_sector_mapping.py 数据源
# ===========================================================================

def fetch_all_stock_industry_map(max_retries: int = 3) -> Dict[str, dict]:
    """
    全量拉取所有 A 股的东财三级行业归属。

    数据源：东财 datacenter RPT_F10_BASIC_ORGINFO（不反爬，0.2s/页，全量 ~70s）
    BOARD_NAME_LEVEL 形如 "机械设备-轨交设备Ⅱ-轨交设备Ⅲ"。

    Returns:
        {stock_code: {"name", "industry_l1", "industry_l2", "industry_l3", "full", "csrc"}}
        拉取失败返回空 dict
    """
    import requests

    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    result: Dict[str, dict] = {}
    page = 1
    total_pages = 0

    try:
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
                                     headers={"User-Agent": UA,
                                              "Referer": "https://emweb.securities.eastmoney.com/"})
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("result") and data["result"].get("data"):
                            rows = data["result"]["data"]
                            total_pages = data["result"].get("pages", 0)
                            for row in rows:
                                code = str(row.get("SECURITY_CODE", ""))
                                if not code or len(code) < 6:
                                    continue
                                board_level = str(row.get("BOARD_NAME_LEVEL", "") or "")
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
                        logger.warning("datacenter 无数据，页 %d: %s", page, data.get("message", ""))
                        break
                    logger.warning("datacenter 状态码 %d，页 %d", r.status_code, page)
                except Exception as e:
                    logger.warning("datacenter 异常，页 %d，重试 %d/%d: %s",
                                   page, attempt + 1, max_retries, str(e)[:60])
                    time.sleep(1)

            if not success:
                logger.warning("datacenter 页 %d 拉取失败，已有 %d 只，停止", page, len(result))
                break

            if page % 50 == 0 or page == 1:
                logger.info("datacenter 全量拉取进度: 页 %d/%d, 已获取 %d 只",
                            page, total_pages, len(result))
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.1)

        logger.info("全量 A 股行业映射(datacenter): %d 只", len(result))
    except Exception as e:
        logger.warning("全量 A 股行业映射拉取失败: %s", str(e)[:100])
    return result


def build_stock_to_sectors_from_industry_map(
    industry_map: Dict[str, dict],
) -> Dict[str, List[str]]:
    """
    东财行业归属 → {code: [ths_key, ...]} 聚合反查索引。

    东财 l2/l1 行业名 → THS 板块（normalize_sector 别名表/名称包含匹配）。
    匹配失败的个股归入"未归类"（Step2 对该股票显示 unknown → 走降级链）。

    Returns:
        {stock_code: [ths_sector_key, ...]}
    """
    from ..data_layer.sw_industry import normalize_sector, _load_ths_industries, THS_INDUSTRIES
    _load_ths_industries()

    name_to_key = {name: code for code, name in THS_INDUSTRIES.items()}
    result: Dict[str, List[str]] = {}
    matched = 0
    unmatched = 0

    def _resolve(industry_name: str) -> Optional[str]:
        if not industry_name:
            return None
        # 1. normalize_sector（别名表 + 名称匹配）
        code = normalize_sector(industry_name)
        if code:
            return code
        # 2. THS 名包含匹配
        for ths_name, ths_key in name_to_key.items():
            if len(industry_name) >= 2 and (industry_name in ths_name or ths_name in industry_name):
                return ths_key
        return None

    for code, info in industry_map.items():
        keys = []
        # 优先 l2（东财二级，与 push2 板块同一体系），降级 l1
        for cand in (info.get("industry_l2"), info.get("industry_l1")):
            key = _resolve(cand)
            if key and key not in keys:
                keys.append(key)
        if keys:
            result[code] = keys
            matched += 1
        else:
            unmatched += 1

    logger.info("东财→THS 行业映射聚合: %d 只命中, %d 只未归类(将走降级)",
                matched, unmatched)
    return result


# ===========================================================================
# 成分股并发拉取
# ===========================================================================

def _extract_code(raw) -> str:
    """提取 6 位股票代码（兼容 sh600519 / 600519 / 600519.SH）"""
    s = str(raw).strip()
    if len(s) >= 2 and s[:2].lower() in ("sh", "sz"):
        s = s[2:]
    s = s.split(".")[0]
    return s[:6]


def fetch_components_concurrent(
    industries: Dict[str, str],
    max_workers: int = 8,
    per_timeout: float = 30.0,
) -> Dict[str, List[str]]:
    """
    并发拉取各板块成分股。

    Args:
        industries: {sector_key: 板块名}（THS 90 行业）
        max_workers: 并发数
        per_timeout: 单板块超时（秒）

    Returns:
        {sector_key: [6位代码, ...]}；失败板块返回空列表并登记日志
    """
    import akshare as ak

    def _fetch_one(key: str, name: str) -> List[str]:
        try:
            df = ak.stock_board_industry_cons_em(symbol=name)
            if df is None or df.empty:
                return []
            code_col = "代码" if "代码" in df.columns else df.columns[1]
            codes = []
            seen = set()
            for _, row in df.iterrows():
                c = _extract_code(row[code_col])
                if len(c) == 6 and c not in seen:
                    seen.add(c)
                    codes.append(c)
            return codes
        except Exception as e:
            logger.warning("板块成分股拉取失败 '%s'(%s): %s", name, key, str(e)[:80])
            return []

    result: Dict[str, List[str]] = {k: [] for k in industries}
    failed: List[str] = []
    items = list(industries.items())

    if not items:
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, k, n): k for k, n in items}
        for future in as_completed(futures):
            key = futures[future]
            try:
                codes = future.result(timeout=per_timeout + 10)
                if codes:
                    result[key] = codes
                else:
                    failed.append(key)
            except Exception as e:
                logger.warning("板块成分股 %s 超时/异常: %s", key, str(e)[:80])
                failed.append(key)

    ok = sum(1 for v in result.values() if v)
    logger.info("成分股并发拉取: %d/%d 板块成功%s",
                ok, len(items),
                f", 失败 {len(failed)}: {','.join(failed[:10])}" if failed else "")
    return result


# ===========================================================================
# K 线轻量指标（优先 ths_cache 历史，零 API）
# ===========================================================================

def compute_sector_metrics_from_kline(name: str) -> Dict:
    """
    从 K 线计算板块轻量指标（3d/5d 涨跌幅 + MA 均线）。

    数据源优先级：ths_cache 历史 parquet（零 API）→ fetch_ths_kline（API 兜底）。

    Returns:
        {change_3d, change_5d, ma_alignment, sector_above_ma20, kline_days} 或空 dict
    """
    closes: List[float] = []

    # 1. ths_cache 历史（S 系列 A2 已落盘 2020 至今，零 API）
    try:
        from ..data_layer.ths_cache import load_history
        df = load_history("industry", name)
        if df is not None and len(df) >= 4 and "close" in df.columns:
            closes = [float(x) for x in df["close"].tolist() if x == x]
    except Exception:
        closes = []

    # 2. API 兜底（同花顺行业指数 K 线，按名称拉；与 sector_ranker._classify_by_ths_industry_kline 一致）
    if len(closes) < 4:
        try:
            import akshare as ak
            from datetime import datetime, timedelta
            df = ak.stock_board_industry_index_ths(
                symbol=name,
                start_date=(datetime.now() - timedelta(days=60)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
            )
            if df is not None and not df.empty:
                close_col = "收盘价" if "收盘价" in df.columns else (
                    "收盘" if "收盘" in df.columns else "close")
                closes = [float(x) for x in df[close_col].tolist() if x == x]
        except Exception as e:
            logger.debug("同花顺行业 K 线兜底失败 '%s': %s", name, str(e)[:60])

    if len(closes) < 4:
        return {}

    metrics: Dict = {}
    metrics["change_3d"] = round((closes[-1] - closes[-4]) / closes[-4] * 100, 2) if closes[-4] else 0.0
    if len(closes) >= 6:
        metrics["change_5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2) if closes[-6] else 0.0

    if len(closes) >= 20:
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        current = closes[-1]
        metrics["sector_above_ma20"] = current > ma20
        if ma5 > ma10 > ma20:
            metrics["ma_alignment"] = "bullish"
        elif ma5 < ma10 < ma20:
            metrics["ma_alignment"] = "bearish"
        else:
            metrics["ma_alignment"] = "cross"
    else:
        metrics["ma_alignment"] = "cross"
        metrics["sector_above_ma20"] = False

    metrics["kline_days"] = len(closes)
    return metrics


def classify_sector_status(metrics: Dict) -> str:
    """
    轻量分类（与 sector_ranker._compute_sw_fallback 规则一致）：
      bullish + 站上 MA20 → main_trend
      bearish + 跌破 MA20 → retreating
      其他 → rotational
    """
    ma_align = metrics.get("ma_alignment", "cross")
    above = metrics.get("sector_above_ma20", False)
    if ma_align == "bullish" and above:
        return "main_trend"
    if ma_align == "bearish" and not above:
        return "retreating"
    return "rotational"
