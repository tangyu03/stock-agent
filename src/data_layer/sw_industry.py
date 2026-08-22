"""
行业数据层（同花顺行业体系）

基于同花顺（THS）行业分类，替代原有申万（SW）体系。
- THS_INDUSTRIES: 90个同花顺子行业（代码 881xxx → 名称）
- normalize_sector: 将常见板块名/别名映射为 THS 代码
- calc_sector_metrics: 计算行业板块分类指标

数据源：
- AKShare: 涨停池(stock_zt_pool_em)、THS行业K线(stock_board_industry_index_ths)、
  板块成分股(stock_board_industry_cons_em)
- 问财 OpenAPI: 行业资金流（主力净流入）
"""

import logging
import time
from typing import Dict, Any, Optional, List, Set
from datetime import datetime, timedelta

from .akshare_adapter import get_akshare_adapter

logger = logging.getLogger(__name__)


def _extract_code(raw) -> str:
    """提取6位股票代码（兼容 sh600519 / 600519 / 600519.SH 等格式）"""
    s = str(raw).strip()
    if len(s) >= 2 and s[:2].lower() in ('sh', 'sz'):
        s = s[2:]
    s = s.split('.')[0]
    return s[:6]


# ===========================================================================
# 同花顺行业映射表（90个行业，从 AKShare stock_board_industry_name_ths 动态加载）
# 代码格式：881xxx，与 AKShare stock_board_industry_index_ths 兼容
# ===========================================================================

# 静态降级数据（AKShare 接口异常时使用，定期从 API 同步更新）
_THS_INDUSTRIES: Dict[str, str] = {
    "881121": "半导体",          "881273": "白酒",
    "881131": "白色家电",        "881156": "保险",
    "881138": "包装印刷",        "881174": "厨卫电器",
    "881281": "电池",            "881277": "电机",
    "881145": "电力",            "881278": "电网设备",
    "881129": "电子化学品",      "881164": "房地产开发",
    "881170": "房地产服务",      "881159": "房屋建设",
    "881141": "纺织制造",        "881276": "风电设备",
    "881135": "服装家纺",        "881166": "钢铁",
    "881167": "港口航运",        "881144": "高速公路",
    "881181": "工程机械",        "881177": "公路运输",
    "881117": "工业金属",        "881282": "光伏设备",
    "881152": "光学光电子",      "881109": "广告营销",
    "881185": "轨交设备",        "881123": "贵金属",
    "881113": "航空运输",        "881160": "航天装备",
    "881175": "航海装备",        "881193": "化妆品",
    "881192": "环保",            "881148": "环境治理",
    "881171": "化学原料",        "881172": "化学制品",
    "881130": "化学制药",        "881246": "计算机设备",
    "881158": "基础建设",        "881136": "家居用品",
    "881111": "家用电器",        "881179": "建材",
    "881142": "建筑装饰",        "881183": "焦炭加工",
    "881128": "教育",            "881105": "酒店餐饮",
    "881103": "军工电子",        "881125": "军工装备",
    "881151": "快递",            "881270": "炼化及贸易",
    "881124": "旅游",            "881173": "铝",
    "881139": "煤炭开采",        "881137": "煤炭开采加工",
    "881112": "美容护理",        "881107": "摩托车及其他",
    "881155": "能源金属",        "881143": "农产品加工",
    "881120": "农化制品",        "881101": "农林牧渔",
    "881188": "普钢",            "881194": "其他发电设备",
    "881195": "其他专用设备",    "881134": "汽车零部件",
    "881116": "软件服务",        "881269": "商用车",
    "881153": "商业物业经营",    "881127": "食品加工",
    "881132": "饰品",            "881184": "水泥",
    "881115": "塑料",            "881108": "特钢",
    "881114": "调味品",          "881165": "通信服务",
    "881102": "通信设备",        "881150": "通用设备",
    "881191": "铜",              "881176": "涂料涂漆",
    "881106": "物流",            "881157": "橡胶",
    "881161": "消费电子",        "881122": "小家电",
    "881182": "小金属",          "881140": "休闲食品",
    "881168": "养殖业",          "881119": "医疗器械",
    "881118": "医疗服务",        "881163": "医药商业",
    "881187": "饮料乳品",        "881147": "印制电路板",
    "881190": "游戏",            "881126": "有色冶炼加工",
    "881162": "娱乐",            "881133": "造纸",
    "881178": "证券",            "881149": "中药",
    "881110": "专业工程",        "881146": "自动化设备",
    "881154": "装修装饰",        "881189": "资产管理",
}

# 代码 → 名称
THS_INDUSTRIES: Dict[str, str] = {}
# 名称 → 代码 反查
THS_NAME_TO_CODE: Dict[str, str] = {}
# 是否已从 AKShare 动态加载
_ths_loaded: bool = False


def _load_ths_industries(force: bool = False) -> None:
    """
    从 AKShare 动态加载同花顺行业列表。

    仅在首次调用时加载，后续使用缓存。加载失败则使用静态降级数据。
    """
    global THS_INDUSTRIES, THS_NAME_TO_CODE, _ths_loaded
    if _ths_loaded and not force:
        return

    try:
        import akshare as ak
        df = ak.stock_board_industry_name_ths()
        if df is not None and not df.empty:
            THS_INDUSTRIES.clear()
            THS_NAME_TO_CODE.clear()
            for _, row in df.iterrows():
                code = str(row["code"])
                name = str(row["name"])
                THS_INDUSTRIES[code] = name
                THS_NAME_TO_CODE[name] = code
            _ths_loaded = True
            logger.info("THS 行业列表已加载: %d 个行业", len(THS_INDUSTRIES))
            return
    except Exception as e:
        logger.warning("AKShare 加载 THS 行业列表失败: %s，使用静态降级数据", e)

    # 降级：使用静态数据
    THS_INDUSTRIES = dict(_THS_INDUSTRIES)
    THS_NAME_TO_CODE = {v: k for k, v in THS_INDUSTRIES.items()}
    _ths_loaded = True
    logger.info("THS 行业列表已加载（静态降级数据）: %d 个行业", len(THS_INDUSTRIES))


# ---- 向后兼容别名（旧代码引用 SW_LEVEL1 不会报错） ----
def _get_sw_level1() -> Dict[str, str]:
    """向后兼容：SW_LEVEL1 现在指向 THS_INDUSTRIES"""
    _load_ths_industries()
    return THS_INDUSTRIES


def _get_sw_name_to_code() -> Dict[str, str]:
    """向后兼容：SW_NAME_TO_CODE 现在指向 THS_NAME_TO_CODE"""
    _load_ths_industries()
    return THS_NAME_TO_CODE


# 延迟评估的向后兼容属性 — 通过模块级 __getattr__ 实现
_sw_compat_map: Dict[str, Any] = {}

# 常用板块别名 → THS 行业名
_SECTOR_ALIASES: Dict[str, str] = {
    "半导体": "半导体", "芯片": "半导体", "集成电路": "半导体", "封测": "半导体",
    "存储": "半导体", "存储芯片": "半导体", "GPU": "半导体", "CPU": "半导体",
    "消费电子": "消费电子", "面板": "光学光电子", "光学光电子": "光学光电子",
    "半导体设备": "半导体", "印制电路板": "印制电路板", "PCB": "印制电路板",
    "先进封装": "半导体", "HBM": "半导体",
    "光刻机": "半导体", "第三代半导体": "半导体", "碳化硅": "半导体",
    "新能源": "电池", "光伏": "光伏设备", "锂电": "电池",
    "储能": "电池", "风电": "风电设备", "充电桩": "电网设备",
    "电池": "电池",
    "人工智能": "软件服务", "AI": "软件服务", "大模型": "软件服务",
    "算力": "软件服务", "信创": "软件服务",
    "软件": "软件服务", "数据要素": "软件服务", "云服务": "软件服务",
    "军工": "军工装备", "航天": "航天装备", "航空": "航空运输",
    "兵器": "军工装备", "船舶": "航海装备",
    "无人机": "军工装备", "商业航天": "航天装备", "低空经济": "军工装备",
    "卫星互联网": "通信设备", "军民融合": "军工装备",
    "医药": "化学制药", "创新药": "化学制药", "CXO": "医疗服务",
    "医疗器械": "医疗器械", "中药": "中药", "疫苗": "化学制药",
    "生物制品": "化学制药", "合成生物": "化学制药",
    "化学制药": "化学制药", "医疗服务": "医疗服务",
    "券商": "证券", "保险": "保险",
    "白酒": "白酒", "啤酒": "饮料乳品", "调味品": "调味品",
    "新能源车": "汽车零部件", "整车": "汽车零部件", "汽车零部件": "汽车零部件",
    "钢铁": "钢铁", "有色": "有色冶炼加工", "黄金": "贵金属", "稀土": "小金属",
    "化工": "化学制品", "石化": "炼化及贸易", "煤炭": "煤炭开采", "建材": "建材",
    "化学原料": "化学原料", "化学制品": "化学制品",
    "塑料": "塑料", "橡胶": "橡胶", "农药": "农化制品",
    "房地产": "房地产开发", "地产": "房地产开发",
    "通信": "通信设备", "5G": "通信设备", "光通信": "通信设备", "光模块": "通信设备",
    "光纤": "通信设备", "CPO": "通信设备", "6G": "通信设备", "卫星通信": "通信设备",
    "传媒": "娱乐", "游戏": "游戏", "影视": "娱乐", "广告营销": "广告营销",
    "电力": "电力", "水务": "环境治理", "燃气": "环境治理",
    "纺织服装": "服装家纺", "服装": "服装家纺",
    "机械设备": "通用设备", "工程机械": "工程机械",
    "机器人": "自动化设备", "人形机器人": "自动化设备",
    "自动化设备": "自动化设备", "工业母机": "自动化设备",
    "旅游": "旅游", "酒店": "酒店餐饮", "餐饮": "酒店餐饮",
    "交通运输": "物流", "航空运输": "航空运输", "港口": "港口航运",
    "环保": "环保", "节能": "环保",
    "锂矿": "能源金属", "磷化工": "农化制品",
}


# ===========================================================================
# normalize_sector — 板块名/别名 → THS 代码
# ===========================================================================

def normalize_sector(name: str, prefer_level2: bool = True) -> Optional[str]:
    """
    将板块名/别名映射为同花顺行业代码。

    Args:
        name: 板块名称，如 "半导体"、"新能源"、"AI"
        prefer_level2: 无意义（保留参数兼容历史调用）

    Returns:
        THS 代码（如 "881121"），无法识别时返回 None
    """
    if not name or not isinstance(name, str):
        return None

    name = name.strip()
    _load_ths_industries()

    # 1. 直接匹配 THS 行业名
    if name in THS_NAME_TO_CODE:
        return THS_NAME_TO_CODE[name]

    # 2. 查别名表
    if name in _SECTOR_ALIASES:
        ths_name = _SECTOR_ALIASES[name]
        if ths_name in THS_NAME_TO_CODE:
            return THS_NAME_TO_CODE[ths_name]

    # 3. THS 名包含关系
    for ths_name, ths_code in THS_NAME_TO_CODE.items():
        if ths_name in name or name in ths_name:
            return ths_code

    # 4. 别名包含关系
    for alias, ths_name in _SECTOR_ALIASES.items():
        if alias in name or name in alias:
            if ths_name in THS_NAME_TO_CODE:
                return THS_NAME_TO_CODE[ths_name]

    logger.debug("无法识别板块: %s", name)
    return None


# ===========================================================================
# 涨停池统计
# ===========================================================================

def fetch_zt_pool(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取指定日期的涨停池"""
    try:
        import akshare as ak
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=date)
        if df is None or df.empty:
            return []
        return df.to_dict("records")
    except Exception as e:
        logger.warning("涨停池获取失败: %s", e)
        return []


def count_zt_by_sector(
    zt_pool: List[Dict[str, Any]],
    ths_code: str,
) -> int:
    """
    统计涨停池中属于指定 THS 行业的股票数。

    Args:
        zt_pool: 涨停池列表（来自 fetch_zt_pool）
        ths_code: THS 行业代码
    """
    _load_ths_industries()
    ths_name = THS_INDUSTRIES.get(ths_code)
    if not ths_name:
        return 0

    count = 0
    for item in zt_pool:
        if not isinstance(item, dict):
            continue
        industry = str(item.get("所属行业", "") or item.get("行业", ""))
        if not industry:
            continue
        if industry == ths_name:
            count += 1
            continue
        normalized = normalize_sector(industry)
        if normalized == ths_code:
            count += 1
    return count


# ===========================================================================
# THS 行业K线拉取
# ===========================================================================

# K线缓存
_kline_cache: Dict[str, List[Dict[str, Any]]] = {}
_kline_cache_date: Optional[str] = None


def fetch_ths_kline(ths_code: str, days: int = 30) -> List[Dict[str, Any]]:
    """
    拉取同花顺行业指数 K 线。

    数据源：AKShare stock_board_industry_index_ths
    缓存：当日有效

    Args:
        ths_code: THS 行业代码，如 "881121"
        days: 拉取近 days 个交易日

    Returns:
        K线列表，按日期升序，字段含 "date", "close", "open", "high", "low", "amount"
    """
    global _kline_cache, _kline_cache_date

    today = datetime.now().strftime("%Y-%m-%d")
    if _kline_cache_date != today:
        _kline_cache.clear()
        _kline_cache_date = today

    cache_key = f"{ths_code}:{days}"
    if cache_key in _kline_cache:
        return _kline_cache[cache_key]

    _load_ths_industries()
    ths_name = THS_INDUSTRIES.get(ths_code)
    if not ths_name:
        logger.warning("未知 THS 代码: %s", ths_code)
        return []

    try:
        import akshare as ak
        start = (datetime.now() - timedelta(days=max(days * 2, 60))).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        df = ak.stock_board_industry_index_ths(
            symbol=ths_name,
            start_date=start,
            end_date=end,
        )
        if df is None or df.empty:
            return []

        df = df.tail(days)
        records = []
        for _, row in df.iterrows():
            records.append({
                "date": str(row["日期"])[:10],
                "open": float(row["开盘价"]),
                "high": float(row["最高价"]),
                "low": float(row["最低价"]),
                "close": float(row["收盘价"]),
                "vol": float(row["成交量"]),
                "amount": float(row["成交额"]),
            })
        _kline_cache[cache_key] = records
        return records

    except Exception as e:
        logger.warning("拉取 THS 行业 K 线失败 %s(%s): %s", ths_code, ths_name, e)
        return []


# 向后兼容别名
fetch_sw_kline = fetch_ths_kline


# ===========================================================================
# 行业资金流（问财 OpenAPI）
# ===========================================================================

_fund_flow_cache: Optional[List[Dict]] = None
_fund_flow_cache_date: Optional[str] = None


def _get_industry_fund_flow_cached() -> Optional[List[Dict]]:
    """
    获取全行业资金流（当日缓存一次，走问财 OpenAPI）。

    Returns:
        [{"行业": "半导体", "THS代码": "881121", "主力净流入": 9438459000.0}, ...]
        或 None
    """
    global _fund_flow_cache, _fund_flow_cache_date

    today = datetime.now().strftime("%Y-%m-%d")
    if _fund_flow_cache is not None and _fund_flow_cache_date == today:
        return _fund_flow_cache

    try:
        from .iwencai_api import query_industry_fund_flow
        data = query_industry_fund_flow()
        if data:
            # 附上 THS 代码（通过名称匹配）
            _load_ths_industries()
            for item in data:
                industry_name = str(item.get("行业", ""))
                if industry_name and industry_name in THS_NAME_TO_CODE:
                    item["THS代码"] = THS_NAME_TO_CODE[industry_name]
            _fund_flow_cache = data
            _fund_flow_cache_date = today
            return data
        logger.debug("全行业资金流：问财返回空数据")
        return None
    except Exception as e:
        logger.debug("全行业资金流获取失败: %s", str(e)[:80])
        return None


# ===========================================================================
# 行业指标计算
# ===========================================================================

def _set_if_present(metrics: dict, source: dict, key: str, *source_keys: str):
    """安全地从 source 取值写入 metrics"""
    for sk in source_keys:
        v = source.get(sk)
        if v is not None and v != "" and v != "-":
            try:
                metrics[key] = float(v) if not isinstance(v, (int, float, bool)) else v
            except (ValueError, TypeError):
                metrics[key] = v
            return


def calc_sector_metrics(ths_code: str) -> Dict[str, Any]:
    """
    计算同花顺行业板块分类指标。

    数据优先级：问财 API（涨跌幅 + 资金流） > K线（MA均线 + 成交额变化）

    Args:
        ths_code: THS 行业代码，如 "881121"

    Returns:
        {ths_code, ths_name, sector_change_3d, sector_change_5d, ma_alignment, ...}
    """
    _load_ths_industries()
    ths_name = THS_INDUSTRIES.get(ths_code, "")
    metrics: Dict[str, Any] = {"ths_code": ths_code, "ths_name": ths_name}

    # ---- 问财：当日涨跌幅 + 资金流 + 涨停家数 + 成分股数（一次查询）----
    fund_list = _get_industry_fund_flow_cached()
    iwencai_item = None
    if fund_list and ths_name:
        for item in fund_list:
            industry = str(item.get("行业", ""))
            ths_code_from_fund = item.get("THS代码", "")
            if ths_code_from_fund == ths_code or industry == ths_name:
                iwencai_item = item
                break

    if iwencai_item:
        _set_if_present(metrics, iwencai_item, "sector_change_1d", "最新涨跌幅:前复权")
        _set_if_present(metrics, iwencai_item, "real_fund_flow", "主力净流入")
        _set_if_present(metrics, iwencai_item, "limit_up_count", "涨停家数")
        _set_if_present(metrics, iwencai_item, "total_stocks", "成份股数量")
        # 内部热度
        zt = metrics.get("limit_up_count")
        ts = metrics.get("total_stocks")
        if zt is not None and ts and ts > 0:
            metrics["internal_heat"] = round(zt / ts, 4)

    # ---- K线：3d/5d涨跌幅 + MA均线（问财组合查询不稳定，K线更可靠）----
    closes: List[float] = []
    amounts: List[float] = []
    kline = fetch_ths_kline(ths_code, days=30)
    if kline and len(kline) >= 5:
        try:
            for k in kline:
                closes.append(float(k.get("close", 0)))
                amounts.append(float(k.get("amount", 0)))

            if len(closes) >= 4:
                metrics["sector_change_3d"] = round((closes[-1] - closes[-4]) / closes[-4] * 100, 2)
            if len(closes) >= 6:
                metrics["sector_change_5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

            if len(closes) >= 20:
                ma5 = sum(closes[-5:]) / 5
                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                current = closes[-1]

                metrics["sector_above_ma20"] = current > ma20
                if current > ma10:
                    metrics["sector_ma_position"] = "above_ma10"
                elif current >= ma20:
                    metrics["sector_ma_position"] = "between_ma10_ma20"
                else:
                    metrics["sector_ma_position"] = "below_ma20"

                if ma5 > ma10 > ma20:
                    metrics["ma_alignment"] = "bullish"
                elif ma5 < ma10 < ma20:
                    metrics["ma_alignment"] = "bearish"
                else:
                    metrics["ma_alignment"] = "cross"

                above_ma5_days = 0
                for i in range(-1, -min(6, len(closes) + 1), -1):
                    if closes[i] > ma5:
                        above_ma5_days += 1
                    else:
                        break
                metrics["consecutive_above_ma5"] = above_ma5_days

            if len(amounts) >= 10:
                recent = sum(amounts[-5:]) / 5
                prev = sum(amounts[-10:-5]) / 5
                metrics["sector_fund_flow_5d"] = round(
                    (recent - prev) / prev * 100, 2
                ) if prev > 0 else 0.0
        except (ValueError, TypeError, IndexError) as e:
            logger.debug("THS行业 %s K线计算失败: %s", ths_code, e)

    # ---- 涨停池兜底（问财未返回时）----
    if "limit_up_count" not in metrics:
        try:
            zt_pool = fetch_zt_pool()
            if zt_pool:
                metrics["limit_up_count"] = count_zt_by_sector(zt_pool, ths_code)
                total_stocks = _get_sector_stock_count(ths_code)
                if total_stocks > 0:
                    metrics["internal_heat"] = round(metrics["limit_up_count"] / total_stocks, 4)
        except Exception as e:
            logger.debug("涨停池统计失败 ths_code=%s: %s", ths_code, e)

    # ---- 兜底 ----
    metrics.setdefault("sector_fund_flow_5d", 0.0)
    metrics.setdefault("sector_change_3d", 0.0)
    metrics.setdefault("sector_change_5d", 0.0)
    metrics.setdefault("sector_above_ma20", False)
    metrics.setdefault("ma_alignment", "cross")

    logger.debug(
        "THS %s(%s): change_1d=%s change_3d=%s change_5d=%s fund=%s align=%s zt=%s",
        ths_code, ths_name,
        metrics.get("sector_change_1d"), metrics.get("sector_change_3d"),
        metrics.get("sector_change_5d"),
        f"{metrics.get('real_fund_flow', 0)/1e8:.1f}亿" if metrics.get("real_fund_flow") else "无",
        metrics.get("ma_alignment", "无"),
        metrics.get("limit_up_count", 0),
    )
    return metrics


# ===========================================================================
# 概念板块指标计算
# ===========================================================================

_concept_kline_cache: Dict[str, List[Dict]] = {}


def calc_concept_metrics(concept_name: str) -> Dict[str, Any]:
    """
    计算概念板块分类指标（与 calc_sector_metrics 同等评分维度）。
    使用 AKShare stock_board_concept_hist_em 获取概念指数K线。
    """
    metrics: Dict[str, Any] = {"concept_name": concept_name}
    closes: List[float] = []
    amounts: List[float] = []

    if concept_name not in _concept_kline_cache:
        try:
            import akshare as ak
            df = ak.stock_board_concept_hist_em(
                symbol=concept_name, period="daily",
                start_date=(datetime.now() - timedelta(days=60)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
            )
            if df is not None and not df.empty:
                kline = []
                for _, row in df.iterrows():
                    kline.append({
                        "date": str(row["日期"])[:10] if "日期" in df.columns else "",
                        "close": float(row["收盘"] if "收盘" in df.columns else row.iloc[4]),
                        "amount": float(row["成交额"] if "成交额" in df.columns else (row.iloc[6] if len(df.columns) > 6 else 0)),
                    })
                _concept_kline_cache[concept_name] = kline
        except Exception as e:
            logger.debug("概念板块 %s K线获取失败: %s", concept_name, e)
            _concept_kline_cache[concept_name] = []

    kline = _concept_kline_cache.get(concept_name, [])
    if kline and len(kline) >= 5:
        try:
            for k in kline:
                closes.append(float(k["close"]))
                amounts.append(float(k.get("amount", 0)))

            if len(closes) >= 5:
                if len(closes) >= 4:
                    metrics["sector_change_3d"] = round(
                        (closes[-1] - closes[-4]) / closes[-4] * 100, 2
                    ) if closes[-4] > 0 else 0
                if len(closes) >= 6:
                    metrics["sector_change_5d"] = round(
                        (closes[-1] - closes[-6]) / closes[-6] * 100, 2
                    ) if closes[-6] > 0 else 0

                if len(closes) >= 20:
                    ma5 = sum(closes[-5:]) / 5
                    ma10 = sum(closes[-10:]) / 10
                    ma20 = sum(closes[-20:]) / 20
                    current = closes[-1]

                    metrics["sector_above_ma20"] = current > ma20
                    if current > ma10:
                        metrics["sector_ma_position"] = "above_ma10"
                    elif current >= ma20:
                        metrics["sector_ma_position"] = "between_ma10_ma20"
                    else:
                        metrics["sector_ma_position"] = "below_ma20"

                    if ma5 > ma10 > ma20:
                        metrics["ma_alignment"] = "bullish"
                    elif ma5 < ma10 < ma20:
                        metrics["ma_alignment"] = "bearish"
                    else:
                        metrics["ma_alignment"] = "cross"

                    above_ma5_days = 0
                    for i in range(-1, -min(6, len(closes) + 1), -1):
                        if closes[i] > ma5:
                            above_ma5_days += 1
                        else:
                            break
                    metrics["consecutive_above_ma5"] = above_ma5_days

                if len(amounts) >= 10:
                    recent = sum(amounts[-5:]) / 5
                    prev = sum(amounts[-10:-5]) / 5
                    metrics["sector_fund_flow_5d"] = round(
                        (recent - prev) / prev * 100, 2
                    ) if prev > 0 else 0.0
        except (ValueError, TypeError, IndexError) as e:
            logger.debug("概念板块 %s K线指标计算失败: %s", concept_name, e)

    if "sector_fund_flow_5d" not in metrics:
        metrics["sector_fund_flow_5d"] = 0.0
    metrics.setdefault("limit_up_count", 0)
    metrics.setdefault("internal_heat", 0.0)

    return metrics


# ===========================================================================
# 成分股数量
# ===========================================================================

def _get_sector_stock_count(ths_code: str) -> int:
    """获取 THS 行业成分股数量（带缓存）"""
    if not hasattr(_get_sector_stock_count, "_cache"):
        _get_sector_stock_count._cache = {}
    if ths_code in _get_sector_stock_count._cache:
        return _get_sector_stock_count._cache[ths_code]

    _load_ths_industries()
    ths_name = THS_INDUSTRIES.get(ths_code, "")
    if not ths_name:
        _get_sector_stock_count._cache[ths_code] = 0
        return 0

    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=ths_name)
        if df is not None and not df.empty:
            count = len(df)
            _get_sector_stock_count._cache[ths_code] = count
            return count
    except Exception:
        pass

    _get_sector_stock_count._cache[ths_code] = 0
    return 0


# ===========================================================================
# 个股 → 行业检测
# ===========================================================================

_sector_memory_cache: Dict[str, Optional[str]] = {}
_sector_index: Optional[Dict[str, str]] = None

# session 级失败控制（P1-2 审计 2026-08-18：加指数退避，退避期内不重打接口）
_index_build_disabled: bool = False
_index_fail_count: int = 0
_index_next_retry_ts: float = 0.0
_INDEX_FAIL_THRESHOLD = 5
_BACKOFF_BASE = 5.0      # 首次失败退避 5s
_BACKOFF_MAX = 300.0     # 退避上限 5min


def _backoff_seconds() -> float:
    """指数退避：5s → 10s → 20s → 40s → ... 封顶 300s"""
    return min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** max(0, _index_fail_count - 1)))


def _mark_index_failure(reason: str):
    global _index_build_disabled, _index_fail_count, _index_next_retry_ts
    _index_fail_count += 1
    _index_next_retry_ts = time.time() + _backoff_seconds()
    if _index_fail_count >= _INDEX_FAIL_THRESHOLD and not _index_build_disabled:
        _index_build_disabled = True
        logger.warning("行业索引构建连续失败 %d 次，本 session 短路: %s",
                       _index_fail_count, reason[:80])
    else:
        logger.warning("行业索引构建失败(第 %d 次)，退避 %.0fs 后再试: %s",
                       _index_fail_count, _backoff_seconds(), reason[:80])


def _reset_index_state():
    global _index_build_disabled, _index_fail_count, _index_next_retry_ts, _sector_index
    _index_build_disabled = False
    _index_fail_count = 0
    _index_next_retry_ts = 0.0
    _sector_index = None


def _build_sector_index(target_codes: Optional[List[str]] = None) -> Dict[str, str]:
    """
    构建 THS 行业成分股反查索引。

    数据源：
      1. stock_board_industry_cons_em（逐行业查成分股）
      2. 失败后降级到 stock_individual_info_em（逐个股查行业）

    Returns:
        {stock_code: ths_code} 映射
    """
    global _sector_index, _index_build_disabled, _index_fail_count, _index_next_retry_ts
    if _sector_index is not None and _sector_index:
        return _sector_index

    if _index_build_disabled:
        return _get_sector_index_from_db() or {}

    # P1-2 退避期：上次失败后未到重试时间，用 DB 缓存兜底，不再打接口
    if time.time() < _index_next_retry_ts:
        logger.debug("行业索引退避中（剩余 %.0fs），使用 DB 缓存", _index_next_retry_ts - time.time())
        return _get_sector_index_from_db() or {}

    _load_ths_industries()
    _sector_index = {}

    try:
        import akshare as ak

        # 只查前 30 个成交活跃的行业（避免全量 90 个行业都调 API）
        ths_items = list(THS_INDUSTRIES.items())[:30]
        scanned = 0
        target_set = set(target_codes) if target_codes else set()

        for ths_code, ths_name in ths_items:
            if target_codes and not target_set:
                break
            try:
                df = ak.stock_board_industry_cons_em(symbol=ths_name)
                if df is not None and not df.empty:
                    code_col = "代码" if "代码" in df.columns else df.columns[1]
                    for _, row in df.iterrows():
                        stock_code = _extract_code(row[code_col])
                        if len(stock_code) >= 6:
                            _sector_index[stock_code] = ths_code
                    scanned += 1
            except Exception as e:
                _mark_index_failure(str(e)[:80])
                if _index_build_disabled:
                    break

        logger.info("THS 行业反查索引: 扫描 %d 个行业, %d 只个股",
                    scanned, len(_sector_index))

    except Exception as e:
        logger.warning("THS 行业反查索引构建失败: %s", e)
        _mark_index_failure(str(e)[:80])

    if not _sector_index:
        # 降级：从数据库缓存恢复
        db_cache = _get_sector_index_from_db()
        if db_cache:
            _sector_index = db_cache
            return _sector_index
        _sector_index = None
        return {}

    # P1-2 成功构建：重置失败计数与退避
    if _index_fail_count:
        _index_fail_count = 0
        _index_next_retry_ts = 0.0

    return _sector_index


def _get_sector_index_from_db() -> Optional[Dict[str, str]]:
    """从 SQLite 读取持久化的行业索引"""
    try:
        from ..db import get_connection
        import json
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value FROM data_cache WHERE cache_key='ths_sector_index'"
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row["cache_value"])
    except Exception:
        pass
    return None


def _save_sector_index_to_db(index: Dict[str, str]):
    """持久化行业索引到 SQLite"""
    try:
        from ..db import get_connection
        import json
        conn = get_connection()
        cursor = conn.cursor()
        expiry = (datetime.now() + timedelta(days=7)).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) VALUES (?, ?, ?)",
            ("ths_sector_index", json.dumps(index, ensure_ascii=False), expiry),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def fetch_stock_sector(code: str) -> Optional[str]:
    """
    获取个股所属同花顺行业名称。

    Args:
        code: 6 位股票代码

    Returns:
        THS 行业名称（如 "半导体"），查询失败返回 None
    """
    if not code:
        return None

    # 1. 内存缓存
    if code in _sector_memory_cache:
        return _sector_memory_cache[code]

    # 2. 数据库缓存
    try:
        from ..db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value FROM data_cache WHERE cache_key = ? AND expire_at > datetime('now')",
            (f"sector:{code}",),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            import json
            val = json.loads(row["cache_value"])
            if val:
                _sector_memory_cache[code] = val
                return val
    except Exception:
        pass

    # 3. 板块成分股反查
    _load_ths_industries()
    index = _build_sector_index()
    ths_code = index.get(code)
    result = THS_INDUSTRIES.get(ths_code) if ths_code else None

    # 4. 写入缓存
    _sector_memory_cache[code] = result
    try:
        import json
        from ..db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        expiry = (datetime.now() + timedelta(days=30)).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) VALUES (?, ?, ?)",
            (f"sector:{code}", json.dumps(result, ensure_ascii=False), expiry),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return result


def fetch_sectors_batch(codes: List[str]) -> Dict[str, Optional[str]]:
    """批量获取多只个股的 THS 行业"""
    global _sector_index, _sector_memory_cache
    if _sector_index is None:
        _build_sector_index(target_codes=codes)
    _sector_memory_cache = {}
    results = {}
    for code in codes:
        results[code] = fetch_stock_sector(code)
    hit = sum(1 for v in results.values() if v)
    logger.info("行业自动检测: %d 只, 命中 %d 只", len(codes), hit)
    return results


# ===========================================================================
# 概念板块检测
# ===========================================================================

_concept_index: Optional[Dict[str, List[str]]] = None


def _build_concept_index(max_workers: int = 4) -> Dict[str, List[str]]:
    """构建概念板块成分股反查索引（进程内缓存）"""
    global _concept_index
    if _concept_index is not None:
        return _concept_index

    _concept_index = {}
    try:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        index_lock = threading.Lock()

        def _fetch_one(label: str, cn: str) -> int:
            try:
                df = ak.stock_sector_detail(sector=label)
                if df is not None and not df.empty:
                    code_col = "code" if "code" in df.columns else df.columns[1]
                    with index_lock:
                        for _, r in df.iterrows():
                            sc = _extract_code(r[code_col])
                            if len(sc) >= 6:
                                _concept_index.setdefault(sc, [])
                                if cn not in _concept_index[sc]:
                                    _concept_index[sc].append(cn)
                    return 1
            except Exception:
                pass
            return 0

        # 优先新浪概念板块接口
        try:
            concept_df = ak.stock_sector_spot(indicator="概念")
            if concept_df is not None and not concept_df.empty:
                label_col = "label" if "label" in concept_df.columns else concept_df.columns[0]
                name_col = "板块" if "板块" in concept_df.columns else concept_df.columns[1]
                tasks = [
                    (str(row[label_col]), str(row[name_col]))
                    for _, row in concept_df.head(100).iterrows()
                ]
                scanned = 0
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_fetch_one, l, n): (l, n) for l, n in tasks}
                    for future in as_completed(futures, timeout=120):
                        try:
                            scanned += future.result(timeout=30)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug("新浪概念板块接口失败: %s", e)

        logger.info("概念板块反查索引: %d 只个股", len(_concept_index))
    except Exception as e:
        logger.warning("概念板块反查索引构建失败: %s", e)

    return _concept_index


def fetch_stock_concepts(code: str, max_concepts: int = 5) -> List[str]:
    """获取个股所属概念板块"""
    if not code:
        return []
    index = _build_concept_index()
    concepts = index.get(code, [])
    return concepts[:max_concepts]


# ===========================================================================
# 概念板块趋势判定
# ===========================================================================

_concept_status_cache: Dict[str, Dict] = {}


def fetch_concept_status(concept_name: str) -> Dict:
    """获取概念板块趋势状态（基于均线排列）"""
    if not concept_name:
        return {}
    if concept_name in _concept_status_cache:
        return _concept_status_cache[concept_name]

    result = {}
    try:
        import akshare as ak
        df = None
        for func in [ak.stock_board_concept_hist_em]:
            try:
                df = func(symbol=concept_name, period='daily',
                          start_date='20260401', end_date=datetime.now().strftime('%Y%m%d'))
                if df is not None and not df.empty:
                    break
            except Exception:
                continue

        if df is not None and len(df) >= 20:
            close_col = '收盘' if '收盘' in df.columns else ('close' if 'close' in df.columns else df.columns[3])
            closes = [float(df.iloc[i][close_col]) for i in range(len(df))]

            if len(closes) >= 20:
                ma5 = sum(closes[-5:]) / 5
                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                current = closes[-1]

                if ma5 > ma10 > ma20:
                    result["ma_alignment"] = "bullish"
                elif ma5 < ma10 < ma20:
                    result["ma_alignment"] = "bearish"
                else:
                    result["ma_alignment"] = "cross"
                result["above_ma20"] = current > ma20
                _concept_status_cache[concept_name] = result
    except Exception as e:
        logger.debug("概念板块状态获取失败 %s: %s", concept_name, e)

    return result


def classify_concept_status(ma_alignment: str, above_ma20: bool) -> str:
    """将均线状态映射为分类标签"""
    if ma_alignment == "bullish" and above_ma20:
        return "主线"
    elif ma_alignment == "bearish" and not above_ma20:
        return "退潮"
    elif ma_alignment:
        return "轮动"
    return "未知"


# ===========================================================================
# 个股完整行业信息（向后兼容 fetch_stock_sw_industry_full）
# ===========================================================================

_sw_industry_session_cache: Dict[str, Dict] = {}
_individual_info_disabled: bool = False
_individual_info_fail_count: int = 0
_INDIVIDUAL_INFO_FAIL_THRESHOLD = 5


def _mark_individual_info_failure(reason: str):
    global _individual_info_disabled, _individual_info_fail_count
    _individual_info_fail_count += 1
    if _individual_info_fail_count >= _INDIVIDUAL_INFO_FAIL_THRESHOLD and not _individual_info_disabled:
        _individual_info_disabled = True
        logger.warning("stock_individual_info_em 连续失败 %d 次，本 session 短路: %s",
                       _individual_info_fail_count, reason[:80])


def _mark_individual_info_success():
    global _individual_info_fail_count
    _individual_info_fail_count = 0


def fetch_stock_sw_industry_full(code: str) -> Dict[str, Optional[str]]:
    """
    获取个股完整行业分类（返回 THS 行业名作为 level2）。

    向后兼容：返回值格式与旧版一致，但内容为 THS 行业。
    """
    result = {"level1": None, "level2": None, "level3": None, "stale": False}
    if not code:
        return result

    now = time.time()

    # session 缓存
    cached = _sw_industry_session_cache.get(code)
    if cached and (now - cached["ts"] < 3600):
        return cached["result"]

    _load_ths_industries()

    # 1. 板块成分股反查
    ths_name = fetch_stock_sector(code)
    if ths_name:
        result["level2"] = ths_name
        result["level1"] = ths_name  # THS 只有一级

    # 2. stock_individual_info_em 兜底
    if not result["level2"] and not _individual_info_disabled:
        try:
            import akshare as ak
            for attempt in range(2):
                try:
                    info_df = ak.stock_individual_info_em(symbol=code)
                    if info_df is not None and not info_df.empty and "item" in info_df.columns:
                        for industry_col in ["行业", "所属行业", "东财行业"]:
                            row = info_df[info_df["item"] == industry_col]
                            if not row.empty:
                                ind_name = str(row["value"].iloc[0])
                                if ind_name and ind_name not in ("nan", "--", "None", ""):
                                    result["level2"] = ind_name
                                    result["level1"] = ind_name
                                    _mark_individual_info_success()
                                    break
                    break
                except Exception as e:
                    err_msg = str(e)[:120]
                    if "RemoteDisconnected" in err_msg and attempt == 0:
                        time.sleep(0.5)
                        continue
                    _mark_individual_info_failure(err_msg)
                    break
        except Exception as e:
            logger.debug("stock_individual_info_em 异常 %s: %s", code, e)

    _sw_industry_session_cache[code] = {"result": result, "ts": now}
    return result


# ===========================================================================
# 模块自检
# ===========================================================================

if __name__ == "__main__":
    _load_ths_industries()
    print(f"THS 行业: {len(THS_INDUSTRIES)} 个")
    for code, name in list(THS_INDUSTRIES.items())[:10]:
        print(f"  {code} → {name}")

    print("\nnormalize_sector 测试:")
    for n in ["半导体", "新能源", "人工智能", "军工", "医药", "白酒"]:
        code = normalize_sector(n)
        name = THS_INDUSTRIES.get(code, "N/A") if code else "N/A"
        print(f"  {n} → {code} ({name})")

    print("\nK线测试(881121=半导体):")
    kline = fetch_ths_kline("881121", days=5)
    for k in kline[-3:]:
        print(f"  {k['date']}: close={k['close']:.2f} amount={k['amount']/1e8:.1f}亿")

    print("\n指标测试(881121=半导体):")
    metrics = calc_sector_metrics("881121")
    for k, v in metrics.items():
        print(f"  {k}: {v}")


# ===========================================================================
# 向后兼容别名 — 旧代码引用 SW_LEVEL1/SW_LEVEL2 不报错
# 使用懒加载代理，首次访问时自动从 AKShare 加载 THS 行业列表
# ===========================================================================

class _LazyDict:
    """懒加载字典代理：首次访问时触发加载，之后透明转发"""

    def __init__(self, loader, fallback):
        self._loader = loader
        self._fallback = fallback
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self._loader()
            self._loaded = True

    def __getitem__(self, key):
        self._ensure_loaded()
        return THS_INDUSTRIES[key]

    def __contains__(self, key):
        self._ensure_loaded()
        return key in THS_INDUSTRIES

    def __iter__(self):
        self._ensure_loaded()
        return iter(THS_INDUSTRIES)

    def __len__(self):
        self._ensure_loaded()
        return len(THS_INDUSTRIES)

    def items(self):
        self._ensure_loaded()
        return THS_INDUSTRIES.items()

    def keys(self):
        self._ensure_loaded()
        return THS_INDUSTRIES.keys()

    def values(self):
        self._ensure_loaded()
        return THS_INDUSTRIES.values()

    def get(self, key, default=None):
        self._ensure_loaded()
        return THS_INDUSTRIES.get(key, default)

    def __repr__(self):
        self._ensure_loaded()
        return repr(THS_INDUSTRIES)


class _LazyReverseDict:
    """懒加载 name→code 字典代理"""

    def __init__(self):
        pass

    def _ensure_loaded(self):
        _load_ths_industries()

    def __getitem__(self, key):
        self._ensure_loaded()
        return THS_NAME_TO_CODE[key]

    def __contains__(self, key):
        self._ensure_loaded()
        return key in THS_NAME_TO_CODE

    def items(self):
        self._ensure_loaded()
        return THS_NAME_TO_CODE.items()

    def get(self, key, default=None):
        self._ensure_loaded()
        return THS_NAME_TO_CODE.get(key, default)


# 向后兼容导出
SW_LEVEL1 = _LazyDict(_load_ths_industries, _THS_INDUSTRIES)
SW_LEVEL2 = SW_LEVEL1  # THS 体系不分一级/二级
SW_NAME_TO_CODE = _LazyReverseDict()
SW_LEVEL2_NAME_TO_CODE = SW_NAME_TO_CODE

