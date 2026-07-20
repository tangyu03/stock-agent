"""
申万行业数据层
- SW_LEVEL1: 申万一级行业映射表（31个行业，代码→名称）
- SW_LEVEL2: 申万二级行业映射表（113个行业，代码→名称）
- SW_LEVEL2_NAME_TO_CODE: 二级名称→代码 反查表
- normalize_sector: 将常见板块名/别名映射为申万一级代码
- calc_sector_metrics: 计算单板块分类所需指标（3/5日涨幅、5日资金流入、涨停股数、MA20 状态）

数据源：AKShare
- 涨停池: ak.stock_zt_pool_em(date)
- 申万指数K线: ak.sw_index_daily(symbol=sw_code) 或 ak.index_hist_sw(symbol)
- 板块成分股: ak.sw_index_third_cons(symbol=sw_code)
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from .akshare_adapter import get_akshare_adapter

logger = logging.getLogger(__name__)


def _extract_code(raw) -> str:
    """从API返回提取6位股票代码（兼容 sh600519 / 600519 / 600519.SH 等格式）"""
    s = str(raw).strip()
    # 去掉 sh/sz 前缀
    if len(s) >= 2 and s[:2].lower() in ('sh', 'sz'):
        s = s[2:]
    # 去掉 .SH/.SZ 等后缀，取前6位
    s = s.split('.')[0]
    return s[:6]


# ---------------------------------------------------------------------------
# 申万一级行业映射表（31个，2021版分类）
# 代码格式：801xxx，与 AKShare sw_index_daily 接口兼容
# ---------------------------------------------------------------------------
SW_LEVEL1: Dict[str, str] = {
    "801010": "农林牧渔",
    "801030": "基础化工",
    "801040": "钢铁",
    "801050": "有色金属",
    "801080": "电子",
    "801110": "家用电器",
    "801120": "食品饮料",
    "801130": "纺织服饰",
    "801140": "轻工制造",
    "801150": "医药生物",
    "801160": "公用事业",
    "801170": "交通运输",
    "801180": "房地产",
    "801200": "商贸零售",
    "801210": "社会服务",
    "801230": "综合",
    "801710": "建筑材料",
    "801720": "建筑装饰",
    "801730": "电力设备",
    "801740": "国防军工",
    "801750": "计算机",
    "801760": "传媒",
    "801770": "通信",
    "801780": "银行",
    "801790": "非银金融",
    "801880": "汽车",
    "801890": "机械设备",
    "801950": "煤炭",
    "801960": "石油石化",
    "801970": "环保",
    "801980": "美容护理",
}

# 名称 → 代码 反查表
SW_NAME_TO_CODE: Dict[str, str] = {v: k for k, v in SW_LEVEL1.items()}

# 申万二级行业（113个），用于更精准的板块分类
SW_LEVEL2: Dict[str, str] = {
    "801011": "种植业", "801012": "渔业", "801013": "饲料",
    "801014": "农产品加工", "801015": "养殖业",
    "801031": "化学原料", "801032": "化学制品", "801033": "塑料",
    "801034": "橡胶", "801035": "农化制品",
    "801041": "普钢", "801042": "特钢",
    "801051": "工业金属", "801052": "贵金属", "801053": "小金属",
    "801054": "金属新材料", "801055": "能源金属",
    "801081": "半导体", "801082": "电子化学品", "801083": "消费电子",
    "801084": "光学光电子", "801085": "元件",
    "801111": "白色家电", "801112": "黑色家电", "801113": "小家电", "801114": "家电零部件",
    "801121": "白酒", "801122": "食品加工", "801123": "饮料乳品",
    "801124": "调味品", "801125": "休闲食品",
    "801131": "纺织制造", "801132": "服装家纺", "801133": "饰品",
    "801141": "造纸", "801142": "包装印刷", "801143": "家居用品", "801144": "文娱用品",
    "801151": "化学制药", "801152": "中药", "801153": "生物制品",
    "801154": "医疗器械", "801155": "医疗服务", "801156": "医药商业",
    "801161": "电力", "801162": "燃气", "801163": "水务",
    "801171": "铁路运输", "801172": "公路运输", "801173": "航运港口",
    "801174": "航空运输", "801175": "物流",
    "801181": "房地产开发", "801182": "房地产服务",
    "801201": "一般零售", "801202": "专业连锁", "801203": "贸易",
    "801211": "酒店旅游", "801212": "教育", "801213": "体育",
    "801231": "综合",
    "801711": "水泥", "801712": "玻璃玻纤", "801713": "装饰建材",
    "801721": "房屋建设", "801722": "基础建设", "801723": "专业工程", "801724": "装修装饰",
    "801731": "电机", "801732": "电网设备", "801733": "光伏设备",
    "801734": "风电设备", "801735": "电池", "801736": "其他电源设备",
    "801741": "航空装备", "801742": "航天装备", "801743": "地面兵装",
    "801744": "航海装备", "801745": "军工电子",
    "801751": "软件开发", "801752": "IT服务", "801753": "计算机设备",
    "801761": "游戏", "801762": "广告营销", "801763": "影视院线",
    "801764": "出版", "801765": "数字媒体",
    "801771": "通信服务", "801772": "通信设备",
    "801781": "国有大行", "801782": "股份制银行", "801783": "城商行", "801784": "农商行",
    "801791": "证券", "801792": "保险", "801793": "多元金融",
    "801881": "乘用车", "801882": "商用车", "801883": "汽车零部件", "801884": "摩托车及其他",
    "801891": "通用设备", "801892": "专用设备", "801893": "自动化设备", "801894": "工程机械",
    "801951": "煤炭开采", "801952": "焦炭加工",
    "801961": "油气开采", "801962": "油服工程", "801963": "炼化及贸易",
    "801971": "环境治理", "801972": "环保设备",
    "801981": "化妆品", "801982": "个护用品",
}
SW_LEVEL2_NAME_TO_CODE: Dict[str, str] = {v: k for k, v in SW_LEVEL2.items()}

# 常见板块别名 → 申万一级名称（用于 normalize_sector 模糊匹配）
_SECTOR_ALIASES: Dict[str, str] = {
    "半导体": "半导体", "芯片": "半导体", "集成电路": "半导体", "封测": "半导体",
    "存储": "半导体", "存储芯片": "半导体", "GPU": "半导体", "CPU": "半导体",
    "消费电子": "消费电子", "面板": "光学光电子", "光学光电子": "光学光电子",
    "半导体设备": "半导体", "印制电路板": "元件", "PCB": "元件",
    "先进封装": "半导体", "HBM": "半导体", "激光雷达": "汽车零部件",
    "光刻机": "半导体", "第三代半导体": "半导体", "碳化硅": "半导体",
    "新能源": "电池", "光伏": "光伏设备", "锂电": "电池",
    "储能": "电池", "风电": "风电设备", "充电桩": "电网设备",
    "电池": "电池", "核电": "其他电源设备",
    "人工智能": "软件开发", "AI": "软件开发", "大模型": "软件开发",
    "算力": "IT服务", "智算": "IT服务", "信创": "软件开发",
    "软件": "软件开发", "数据要素": "IT服务", "云服务": "IT服务",
    "军工": "航空装备", "航天": "航天装备", "航空": "航空装备",
    "兵器": "地面兵装", "船舶": "航海装备", "航天军工": "航天装备",
    "无人机": "航空装备", "商业航天": "航天装备", "低空经济": "航空装备",
    "卫星互联网": "通信设备", "军民融合": "航空装备",
    "医药": "化学制药", "创新药": "化学制药", "CXO": "医疗服务",
    "医疗器械": "医疗器械", "中药": "中药", "疫苗": "生物制品",
    "生物制品": "生物制品", "合成生物": "生物制品",
    "化学制药": "化学制药", "医疗服务": "医疗服务",
    "券商": "证券", "保险": "保险", "多元金融": "多元金融",
    "白酒": "白酒", "啤酒": "饮料乳品", "调味品": "调味品",
    "新能源车": "汽车零部件", "整车": "乘用车", "汽车零部件": "汽车零部件",
    "钢铁": "普钢", "有色": "工业金属", "黄金": "贵金属", "稀土": "小金属",
    "化工": "化学制品", "石化": "炼化及贸易", "煤炭": "煤炭开采", "建材": "水泥",
    "化学原料": "化学原料", "化学制品": "化学制品", "化学纤维": "化学制品",
    "塑料": "塑料", "橡胶": "橡胶", "农药": "农化制品",
    "房地产": "房地产开发", "地产": "房地产开发",
    "通信": "通信设备", "5G": "通信设备", "光通信": "通信设备", "光模块": "通信设备",
    "光纤": "通信设备", "CPO": "通信设备", "6G": "通信设备", "卫星通信": "通信设备",
    "传媒": "数字媒体", "游戏": "游戏", "影视": "影视院线", "广告营销": "广告营销",
    "电力": "电力", "水务": "水务", "燃气": "燃气",
    "纺织服装": "服装家纺", "服装": "服装家纺",
    "机械设备": "通用设备", "工程机械": "工程机械",
    "机器人": "自动化设备", "人形机器人": "自动化设备",
    "自动化设备": "自动化设备", "工业母机": "自动化设备",
    "商贸": "一般零售", "零售": "一般零售", "百货": "一般零售",
    "旅游": "酒店旅游", "酒店": "酒店旅游", "餐饮": "酒店旅游",
    "交通运输": "物流", "航空运输": "航空运输", "港口": "航运港口",
    "环保": "环境治理", "节能": "环境治理",
    "锂矿": "能源金属", "磷化工": "农化制品",
}


def normalize_sector(name: str, prefer_level2: bool = True) -> Optional[str]:
    """
    将板块名/别名映射为申万行业代码（优先二级，降级一级）。

    Args:
        name: 板块名称，如 "半导体"、"新能源"、"AI"
        prefer_level2: True 优先返回二级代码，False 返回一级代码

    Returns:
        申万代码（如 "801081" 二级 / "801080" 一级），无法识别时返回 None
    """
    if not name or not isinstance(name, str):
        return None

    name = name.strip()

    if prefer_level2:
        if name in SW_LEVEL2_NAME_TO_CODE:
            return SW_LEVEL2_NAME_TO_CODE[name]
        for sw_name, sw_code in SW_LEVEL2_NAME_TO_CODE.items():
            if sw_name in name:
                return sw_code

    if name in SW_NAME_TO_CODE:
        return SW_NAME_TO_CODE[name]

    if name in _SECTOR_ALIASES:
        sw_name = _SECTOR_ALIASES[name]
        if prefer_level2:
            code = SW_LEVEL2_NAME_TO_CODE.get(sw_name)
            if code:
                return code
        return SW_NAME_TO_CODE.get(sw_name)

    for sw_name, sw_code in SW_NAME_TO_CODE.items():
        if sw_name in name or name in sw_name:
            return sw_code

    for alias, sw_name in _SECTOR_ALIASES.items():
        if alias in name or name in alias:
            if prefer_level2:
                code = SW_LEVEL2_NAME_TO_CODE.get(sw_name)
                if code:
                    return code
            return SW_NAME_TO_CODE.get(sw_name)

    logger.debug("无法识别板块: %s", name)
    return None

# ---------------------------------------------------------------------------
# 涨停池统计
# ---------------------------------------------------------------------------
def fetch_zt_pool(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    获取指定日期的涨停池（直接调 AKShare，不走适配器避免反爬重试延迟）。

    Args:
        date: YYYYMMDD 字符串，None 表示今日

    Returns:
        涨停股列表（每项含 "所属行业" 字段，用于按板块统计）
    """
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
    sw_code: str,
) -> int:
    """
    统计涨停池中属于指定申万一级行业的股票数。

    Args:
        zt_pool: 涨停池列表（来自 fetch_zt_pool）
        sw_code: 申万一级代码

    Returns:
        涨停股数量
    """
    sw_name = SW_LEVEL1.get(sw_code)
    if not sw_name:
        return 0

    count = 0
    for item in zt_pool:
        if not isinstance(item, dict):
            continue
        # 涨停池字段："所属行业"（东财字段名）
        industry = str(item.get("所属行业", "") or item.get("行业", ""))
        if not industry:
            continue
        # 精确匹配
        if industry == sw_name:
            count += 1
            continue
        # 模糊匹配（行业字段可能是细分行业名，需经 normalize_sector 映射）
        normalized = normalize_sector(industry)
        if normalized == sw_code:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 申万指数K线拉取
# ---------------------------------------------------------------------------
def fetch_sw_kline(sw_code: str, days: int = 30) -> List[Dict[str, Any]]:
    """
    拉取申万一级行业指数K线。

    Args:
        sw_code: 申万一级代码，如 "801010"
        days: 拉取近 days 个交易日

    Returns:
        K线列表，按日期升序，字段含 "收盘" / "close"
    """
    try:
        import akshare as ak
        # 用 index_hist_sw（新浪源，稳定），不用 sw_index_daily（东财源，被反爬）
        df = ak.index_hist_sw(symbol=sw_code, period="day")
        if df is None or df.empty:
            return []
        # 取最近 days 天
        df = df.tail(days)
        records = df.to_dict("records")
        normalized = []
        for r in records:
            item = dict(r)
            # 兼容英文列名
            if "close" not in item and "收盘" in item:
                item["close"] = item["收盘"]
            if "date" not in item and "日期" in item:
                item["date"] = item["日期"]
            if "amount" not in item and "成交额" in item:
                item["amount"] = item["成交额"]
            normalized.append(item)
        return normalized
    except Exception as e:
        logger.warning("拉取申万指数K线失败 sw_code=%s: %s", sw_code, e)
        return []


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

# 全行业资金流：当日缓存 + session级失败标记（被封后不再重试）
_fund_flow_cache = None
_fund_flow_cache_date = None
_fund_flow_disabled = False   # 本session内被封/超时后置True，跳过后续所有调用


def _get_industry_fund_flow_cached():
    """
    获取全行业资金流（当日缓存一次，超时5s快速失败，被封后本session不再试）。

    stock_fund_flow_industry 一次返回全部行业，被东财反爬时会静默挂起。
    用线程超时保护 + 缓存，避免每个板块都卡一次。

    Returns:
        pandas.DataFrame 或 None
    """
    global _fund_flow_cache, _fund_flow_cache_date, _fund_flow_disabled

    if _fund_flow_disabled:
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    if _fund_flow_cache is not None and _fund_flow_cache_date == today:
        return _fund_flow_cache

    try:
        import akshare as ak
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(ak.stock_fund_flow_industry, symbol="全部行业")
            df = fut.result(timeout=5)   # 5秒快速失败
        _fund_flow_cache = df
        _fund_flow_cache_date = today
        return df
    except concurrent.futures.TimeoutError:
        logger.warning("全行业资金流接口超时（被反爬），本session跳过 real_fund_flow")
        _fund_flow_disabled = True
        return None
    except Exception as e:
        logger.debug("全行业资金流获取失败，跳过: %s", str(e)[:80])
        _fund_flow_disabled = True
        return None


def calc_sector_metrics(sw_code: str) -> Dict[str, Any]:
    """
    计算申万行业板块的分类指标。

    支持一级和二级代码（如 801080=电子一级, 801081=半导体二级）。

    返回字段：
        - sector_change_3d: float  3日涨跌幅
        - sector_change_5d: float  5日涨跌幅
        - sector_fund_flow_5d: float  成交额变化代理（>0=放量）
        - limit_up_count: int  板块内涨停股数
        - sector_above_ma20: bool  是否站上MA20
        - sector_ma_position: str  MA位置
    """
    metrics: Dict[str, Any] = {}
    closes: List[float] = []
    amounts: List[float] = []

    # 1. K线 → 统一计算涨跌幅 + MA（只算一次）
    kline = fetch_sw_kline(sw_code, days=30)
    if kline and len(kline) >= 5:
        try:
            for k in kline:
                v = k.get("收盘", k.get("close"))
                if v is not None:
                    closes.append(float(v))
                a = k.get("成交额", k.get("amount", 0))
                amounts.append(float(a) if a else 0.0)

            if len(closes) >= 5:
                # 涨跌幅
                if len(closes) >= 4:
                    metrics["sector_change_3d"] = (closes[-1] - closes[-4]) / closes[-4]
                if len(closes) >= 6:
                    metrics["sector_change_5d"] = (closes[-1] - closes[-6]) / closes[-6]

                # MA（只算一次，后续共用）
                if len(closes) >= 20:
                    ma5 = sum(closes[-5:]) / 5
                    ma10 = sum(closes[-10:]) / 10
                    ma20 = sum(closes[-20:]) / 20
                    current = closes[-1]

                    # MA位置（原有）
                    metrics["sector_above_ma20"] = current > ma20
                    if current > ma10:
                        metrics["sector_ma_position"] = "above_ma10"
                    elif current >= ma20:
                        metrics["sector_ma_position"] = "between_ma10_ma20"
                    else:
                        metrics["sector_ma_position"] = "below_ma20"

                    # 均线排列（新增，复用 MA 值）
                    if ma5 > ma10 > ma20:
                        metrics["ma_alignment"] = "bullish"
                    elif ma5 < ma10 < ma20:
                        metrics["ma_alignment"] = "bearish"
                    else:
                        metrics["ma_alignment"] = "cross"

                    # 动量持续性：连续站稳MA5天数（新增，复用 ma5）
                    above_ma5_days = 0
                    for i in range(-1, -6, -1):
                        if closes[i] > ma5:
                            above_ma5_days += 1
                        else:
                            break
                    metrics["consecutive_above_ma5"] = above_ma5_days

                # 资金流代理：5日均成交额 vs 前5日
                if len(amounts) >= 10:
                    recent = sum(amounts[-5:]) / 5
                    prev = sum(amounts[-10:-5]) / 5
                    metrics["sector_fund_flow_5d"] = (recent - prev) / prev if prev > 0 else 0.0
        except (ValueError, TypeError, IndexError) as e:
            logger.debug("申万板块 %s K线指标计算失败: %s", sw_code, e)

    # 2. 涨停池统计
    try:
        zt_pool = fetch_zt_pool()
        if zt_pool:
            metrics["limit_up_count"] = count_zt_by_sector(zt_pool, sw_code)
            total_stocks = _get_sector_stock_count(sw_code)
            if total_stocks > 0:
                metrics["internal_heat"] = metrics["limit_up_count"] / total_stocks
    except Exception as e:
        logger.debug("涨停池统计失败 sw_code=%s: %s", sw_code, e)

    # 3. 资金流兜底
    if "sector_fund_flow_5d" not in metrics:
        metrics["sector_fund_flow_5d"] = 0.0

    # 4. 真实资金流（全行业资金流当日缓存一次，超时/被封则本session跳过）
    fund_df = _get_industry_fund_flow_cached()
    if fund_df is not None and not fund_df.empty:
        try:
            sw_name = SW_LEVEL1.get(sw_code, "")
            if sw_name:
                col_name = "行业" if "行业" in fund_df.columns else fund_df.columns[0]
                fund_col = "主力净流入-净额" if "主力净流入-净额" in fund_df.columns else (
                    "主力净流入" if "主力净流入" in fund_df.columns else None)
                if fund_col:
                    row = fund_df[fund_df[col_name] == sw_name]
                    if row.empty and len(sw_name) >= 2:
                        row = fund_df[fund_df[col_name].str.contains(sw_name[:2], na=False)]
                    if not row.empty:
                        metrics["real_fund_flow"] = float(row.iloc[0][fund_col])
        except Exception:
            pass  # 真实资金流为可选指标，失败不影响主流程

    logger.debug(
        "申万板块 %s 指标: change_3d=%s, change_5d=%s, limit_up=%s, heat=%s, align=%s, ma5days=%s",
        sw_code,
        metrics.get("sector_change_3d"), metrics.get("sector_change_5d"),
        metrics.get("limit_up_count"), metrics.get("internal_heat"),
        metrics.get("ma_alignment"), metrics.get("consecutive_above_ma5"),
    )
    return metrics


# ---------------------------------------------------------------------------
# 概念板块指标计算（与 SW 板块同等的评分体系）
# ---------------------------------------------------------------------------

# 概念板块 K 线缓存（当日有效）
_concept_kline_cache: Dict[str, List[Dict]] = {}


def calc_concept_metrics(concept_name: str) -> Dict[str, Any]:
    """
    计算概念板块的分类指标（与 calc_sector_metrics 同等的评分维度）。

    使用 AKShare stock_board_concept_hist_em 获取概念指数 K 线，
    计算涨跌幅、均线排列、成交额变化等指标。

    Args:
        concept_name: 概念板块名称（如 "半导体"、"ChatGPT概念"）

    Returns:
        与 calc_sector_metrics 相同结构的 metrics dict
    """
    metrics: Dict[str, Any] = {}
    closes: List[float] = []
    amounts: List[float] = []

    # 1. K线 → 涨跌幅 + MA（概念板块指数）
    if concept_name not in _concept_kline_cache:
        try:
            import akshare as ak
            df = ak.stock_board_concept_hist_em(symbol=concept_name, period="daily",
                                                  start_date=(datetime.now() - timedelta(days=60)).strftime("%Y%m%d"),
                                                  end_date=datetime.now().strftime("%Y%m%d"))
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
                    metrics["sector_change_3d"] = (closes[-1] - closes[-4]) / closes[-4] if closes[-4] > 0 else 0
                if len(closes) >= 6:
                    metrics["sector_change_5d"] = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] > 0 else 0

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
                    for i in range(-1, -6, -1):
                        if closes[i] > ma5:
                            above_ma5_days += 1
                        else:
                            break
                    metrics["consecutive_above_ma5"] = above_ma5_days

                if len(amounts) >= 10:
                    recent = sum(amounts[-5:]) / 5
                    prev = sum(amounts[-10:-5]) / 5
                    metrics["sector_fund_flow_5d"] = (recent - prev) / prev if prev > 0 else 0.0
        except (ValueError, TypeError, IndexError) as e:
            logger.debug("概念板块 %s K线指标计算失败: %s", concept_name, e)

    if "sector_fund_flow_5d" not in metrics:
        metrics["sector_fund_flow_5d"] = 0.0

    # 概念板块没有 SW 行业专属的涨停池/资金流 API，这些字段留空
    metrics.setdefault("limit_up_count", 0)
    metrics.setdefault("internal_heat", 0.0)

    return metrics


def _get_sector_stock_count(sw_code: str) -> int:
    """获取申万行业成分股数量（带缓存）"""
    if not hasattr(_get_sector_stock_count, "_cache"):
        _get_sector_stock_count._cache = {}
    if sw_code in _get_sector_stock_count._cache:
        return _get_sector_stock_count._cache[sw_code]
    try:
        sw_name = SW_LEVEL2.get(sw_code) or SW_LEVEL1.get(sw_code, "")
        if sw_name:
            df = ak.stock_board_industry_cons_em(symbol=sw_name)
            if df is not None and not df.empty:
                count = len(df)
                _get_sector_stock_count._cache[sw_code] = count
                return count
    except Exception:
        pass
    _get_sector_stock_count._cache[sw_code] = 0
    return 0


# ---------------------------------------------------------------------------
# 个股行业自动检测（替代人工填写 sector）
# ---------------------------------------------------------------------------

# 内存缓存：{stock_code: sw_level1_code}
_sector_memory_cache: Dict[str, Optional[str]] = {}
# 板块成分股反查索引：{stock_code: sw_level1_code}（懒加载）
_sector_index: Optional[Dict[str, str]] = None

# SW API session 级失败标记（类似 _fund_flow_disabled）
# swsindex.com 返回非 JSON（反爬/错误页）时置 True，本 session 内不再尝试
# 避免每只股票都重试 31 个 SW 板块（N×31 次无效 API 调用）
_sw_api_disabled: bool = False
_sw_api_fail_count: int = 0       # 连续失败计数
_SW_API_FAIL_THRESHOLD = 3        # 连续失败 3 次后短路
_sw_api_disabled_reason: str = ""


def _is_sw_api_disabled() -> bool:
    """检查 SW API 是否已被 session 级短路"""
    return _sw_api_disabled


def _mark_sw_api_failure(reason: str):
    """记录 SW API 失败，连续失败超过阈值后短路本 session"""
    global _sw_api_disabled, _sw_api_fail_count, _sw_api_disabled_reason
    _sw_api_fail_count += 1
    if _sw_api_fail_count >= _SW_API_FAIL_THRESHOLD and not _sw_api_disabled:
        _sw_api_disabled = True
        _sw_api_disabled_reason = reason
        logger.warning(
            "SW API 连续失败 %d 次（最近: %s），本 session 内不再尝试，"
            "上层将仅依赖 stock_individual_info_em 个股级兜底",
            _sw_api_fail_count, reason[:80],
        )


def _reset_sw_api_state():
    """重置 SW API 状态（仅供测试用）"""
    global _sw_api_disabled, _sw_api_fail_count, _sw_api_disabled_reason, _sector_index
    _sw_api_disabled = False
    _sw_api_fail_count = 0
    _sw_api_disabled_reason = ""
    _sector_index = None


def _build_sector_index(target_codes: Optional[List[str]] = None) -> Dict[str, str]:
    """
    构建板块成分股反查索引（只查目标股票涉及的板块）。

    数据源：
      1. 申万一级 index_component_sw（最准，但 swsindex.com 经常被反爬）
      2. 新浪行业 stock_sector_spot + stock_sector_detail（仅 targeted 模式补齐）

    性能保护：
      - SW API 连续失败 3 次后，本 session 内不再尝试（_sw_api_disabled）
      - 避免 14 只股票 × 31 个 SW 板块 = 434 次无效 API 调用
      - 失败时返回空 dict，由上层 fetch_stock_sw_industry_full 走个股级兜底

    Args:
        target_codes: 只查这些股票涉及的板块（如持仓股）。
                      None=查全部 31 个申万一级（慢，不推荐）。

    构建 {stock_code: sw_level1_code} 映射。一次性构建，进程内缓存。
    """
    global _sector_index, _sw_api_disabled
    if _sector_index is not None and _sector_index:
        return _sector_index

    # SW API 已被 session 级短路 → 直接返回空，不再尝试
    if _sw_api_disabled:
        logger.debug("SW API 已短路（%s），跳过 _build_sector_index", _sw_api_disabled_reason[:60])
        return {}

    _sector_index = {}
    fail_reasons: List[str] = []
    sw_api_success_count = 0
    try:
        import akshare as ak

        if target_codes:
            # 只查目标股票涉及的板块：遍历申万一级，找到包含目标股的板块就停
            target_set = set(target_codes)
            found: Dict[str, str] = {}
            scanned = 0

            for sw_code, sw_name in SW_LEVEL1.items():
                if not target_set:
                    break
                try:
                    df = ak.index_component_sw(symbol=sw_code)
                    if df is not None and not df.empty:
                        code_col = "证券代码" if "证券代码" in df.columns else df.columns[1]
                        sector_codes = set(_extract_code(row[code_col]) for _, row in df.iterrows())
                        matched = target_set & sector_codes
                        for code in matched:
                            found[code] = sw_code
                            target_set.discard(code)
                        scanned += 1
                        sw_api_success_count += 1
                    elif len(fail_reasons) < 3:
                        fail_reasons.append(f"{sw_code}({sw_name}): empty")
                        _mark_sw_api_failure(f"{sw_code}: empty")
                except Exception as e:
                    err_msg = str(e)[:60]
                    if len(fail_reasons) < 3:
                        fail_reasons.append(f"{sw_code}({sw_name}): {err_msg}")
                    _mark_sw_api_failure(f"{sw_code}: {err_msg}")
                    # 短路后立即退出循环
                    if _sw_api_disabled:
                        break

            # 对仍未找到的股票，用新浪行业接口逐个查（SW 短路后仍可走这条）
            if target_set and not _sw_api_disabled:
                logger.info("申万一级未命中 %d 只，用新浪行业查: %s",
                            len(target_set), ",".join(sorted(target_set)))
                _fill_from_sina(list(target_set), found)
                target_set = set(c for c in target_set if c not in found)

            _sector_index = found
            logger.info(
                "板块反查索引构建完成（目标股 %d 只）: 命中 %d 只, 扫描 %d 个板块, "
                "失败 %d%s, 未命中 %d, SW短路=%s",
                len(target_codes), len(found), scanned, len(fail_reasons),
                f"({';'.join(fail_reasons)})" if fail_reasons else "",
                len(target_set), _sw_api_disabled,
            )
        else:
            # 全量模式：遍历所有申万一级（慢，备用）
            scanned = 0
            for sw_code, sw_name in SW_LEVEL1.items():
                try:
                    df = ak.index_component_sw(symbol=sw_code)
                    if df is not None and not df.empty:
                        code_col = "证券代码" if "证券代码" in df.columns else df.columns[1]
                        for _, row in df.iterrows():
                            stock_code = _extract_code(row[code_col])
                            if len(stock_code) >= 6:
                                _sector_index[stock_code] = sw_code
                        scanned += 1
                        sw_api_success_count += 1
                    elif len(fail_reasons) < 3:
                        fail_reasons.append(f"{sw_code}({sw_name}): empty")
                        _mark_sw_api_failure(f"{sw_code}: empty")
                except Exception as e:
                    err_msg = str(e)[:60]
                    if len(fail_reasons) < 3:
                        fail_reasons.append(f"{sw_code}({sw_name}): {err_msg}")
                    _mark_sw_api_failure(f"{sw_code}: {err_msg}")
                    if _sw_api_disabled:
                        break

            logger.info(
                "板块反查索引构建完成（全量）: %d 个板块, %d 只个股, 失败 %d%s, SW短路=%s",
                scanned, len(_sector_index), len(fail_reasons),
                f"({';'.join(fail_reasons)})" if fail_reasons else "",
                _sw_api_disabled,
            )

    except Exception as e:
        logger.warning("板块反查索引构建失败: %s", e)
        _mark_sw_api_failure(f"outer: {str(e)[:60]}")

    # 失败（空结果）不缓存，允许下次重试（但 SW 短路后会快速返回空）
    if not _sector_index:
        if not _sw_api_disabled:
            logger.warning(
                "板块反查索引为空但 SW 未短路，不缓存以便重试；"
                "上层将走 stock_individual_info_em 个股级兜底",
            )
        _sector_index = None
        return {}

    return _sector_index


def _fill_from_sina(target_codes: List[str], found: Dict[str, str]):
    """用新浪行业接口补齐申万未命中的股票"""
    try:
        import akshare as ak
        sina_df = ak.stock_sector_spot(indicator="新浪行业")
        if sina_df is None or sina_df.empty:
            return

        sina_label_col = "label" if "label" in sina_df.columns else sina_df.columns[0]
        sina_name_col = "板块" if "板块" in sina_df.columns else sina_df.columns[1]

        # 新浪行业名 → 申万一级代码 映射（用关键词匹配）
        sina_kw_to_sw = [
            ("汽车", "801880"), ("机械", "801890"), ("煤炭", "801950"),
            ("石油", "801960"), ("环保", "801970"), ("美容", "801980"),
            ("化妆品", "801980"),
        ]

        target_set = set(target_codes)
        for _, row in sina_df.iterrows():
            if not target_set:
                break
            sina_name = str(row[sina_name_col])
            label = str(row[sina_label_col])
            # 用关键词匹配申万行业
            sw_code = None
            for kw, sw_val in sina_kw_to_sw:
                if kw in sina_name:
                    sw_code = sw_val
                    break
            if not sw_code:
                continue
            try:
                df = ak.stock_sector_detail(sector=label)
                if df is not None and not df.empty:
                    code_col = "code" if "code" in df.columns else df.columns[1]
                    sector_codes = set(_extract_code(r[code_col]) for _, r in df.iterrows())
                    matched = target_set & sector_codes
                    for code in matched:
                        found[code] = sw_code
                        target_set.discard(code)
            except Exception:
                continue
    except Exception as e:
        logger.debug("新浪行业补齐失败: %s", e)


def fetch_stock_sector(code: str) -> Optional[str]:
    """
    自动获取个股所属申万一级行业（无 API 调用，纯本地反查）

    策略：构建申万一级行业成分股索引 → 反查股票所属行业。
    索引首次使用时从 AKShare 批量拉取（~10 次 API 调用），后续纯内存查询。

    Args:
        code: 6 位股票代码

    Returns:
        申万一级行业名称（如 "电子"），查询失败返回 None
    """
    if not code:
        return None

    # 1. 内存缓存
    if code in _sector_memory_cache:
        return _sector_memory_cache[code]

    # 2. 数据库缓存（只缓存有值的记录，None 不缓存）
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
            if val:  # 只缓存有值的记录，None 不返回（重新查）
                _sector_memory_cache[code] = val
                return val
    except Exception:
        pass

    # 3. 板块成分股反查（首次自动构建索引，~10 次 API 调用）
    index = _build_sector_index()
    sw_code = index.get(code)

    # 4. 转为申万名称
    result = None
    if sw_code:
        result = SW_LEVEL2.get(sw_code) or SW_LEVEL1.get(sw_code)
        # ??????????????????????????????
        if result and not SW_LEVEL2.get(sw_code):
            logger.debug("?? %s ???????? %s???????", code, result)

    # 5. 写入缓存
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
    """
    批量获取多只个股的申万一级行业

    优化：只查这些股票涉及的板块（而非遍历全部 31 个申万一级）。
    传入 codes 后，用 index_component_sw 逐个板块查，找到目标股就提前停止。

    Args:
        codes: 股票代码列表

    Returns:
        {code: 行业名称 或 None}
    """
    # 先用 target_codes 模式构建索引（只查涉及的板块）
    global _sector_index, _sector_memory_cache
    if _sector_index is None:
        _build_sector_index(target_codes=codes)

    # 批量查时清空内存缓存，避免旧 None 缓存影响
    _sector_memory_cache = {}

    results = {}
    for code in codes:
        results[code] = fetch_stock_sector(code)
    hit = sum(1 for v in results.values() if v)
    logger.info("行业自动检测: %d 只, 命中 %d 只", len(codes), hit)
    return results


if __name__ == "__main__":
    # 自检
    for n in ["半导体", "新能源", "人工智能", "军工", "医药", "白酒"]:
        code = normalize_sector(n)
        print(f"{n} → {code} ({SW_LEVEL1.get(code, 'N/A')})")

    # 个股行业检测测试
    print()
    print("个股行业检测:")
    for code in ["688256", "000001"]:
        sector = fetch_stock_sector(code)
        print(f"  {code} → {sector} ({SW_LEVEL1.get(sector or '', 'N/A')})")


# ---------------------------------------------------------------------------
# 概念板块检测（透传到 push）
# ---------------------------------------------------------------------------

# 概念板块反查索引：{stock_code: [概念名称, ...]}
_concept_index: Optional[Dict[str, List[str]]] = None


def _build_concept_index(max_workers: int = 4) -> Dict[str, List[str]]:
    """
    构建概念板块成分股反查索引（ThreadPoolExecutor 并行，进程内缓存）。

    优先使用新浪接口 stock_sector_spot + stock_sector_detail（稳定），
    东财接口 stock_board_concept_name_em 作为备用（常被反爬）。
    构建 {stock_code: [概念名称, ...]} 映射。
    """
    global _concept_index
    if _concept_index is not None:
        return _concept_index

    _concept_index = {}
    try:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        index_lock = threading.Lock()
        scanned = 0

        def _fetch_one_concept(label: str, cn: str) -> int:
            """拉取单个概念的成分股，写入全局索引。返回 1=成功, 0=失败。"""
            try:
                df = ak.stock_sector_detail(sector=label)
                if df is not None and not df.empty:
                    code_col = "code" if "code" in df.columns else df.columns[1]
                    with index_lock:
                        for _, stock_row in df.iterrows():
                            stock_code = _extract_code(stock_row[code_col])
                            if len(stock_code) >= 6:
                                if stock_code not in _concept_index:
                                    _concept_index[stock_code] = []
                                if cn not in _concept_index[stock_code]:
                                    _concept_index[stock_code].append(cn)
                    return 1
            except Exception:
                pass
            return 0

        # 优先：新浪概念板块接口（稳定）
        try:
            concept_df = ak.stock_sector_spot(indicator="概念")
            if concept_df is not None and not concept_df.empty:
                label_col = "label" if "label" in concept_df.columns else concept_df.columns[0]
                name_col = "板块" if "板块" in concept_df.columns else concept_df.columns[1]
                # 取前 100 个概念，并行拉取成分股
                tasks = [
                    (str(row[label_col]), str(row[name_col]))
                    for _, row in concept_df.head(100).iterrows()
                ]
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(_fetch_one_concept, label, cn): (label, cn)
                        for label, cn in tasks
                    }
                    for future in as_completed(futures, timeout=120):
                        try:
                            scanned += future.result(timeout=30)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug("新浪概念板块接口失败: %s", e)

        # 备用：东财概念板块接口
        if scanned == 0:
            logger.warning("新浪概念接口失败，降级到东财接口")
            try:
                concept_df = ak.stock_board_concept_name_em()
                if concept_df is not None and not concept_df.empty:
                    concept_names = concept_df.iloc[:200, 0].tolist() if len(concept_df.columns) > 0 else []

                    def _fetch_one_em(cn: str) -> int:
                        try:
                            df = ak.stock_board_concept_cons_em(symbol=cn)
                            if df is not None and not df.empty:
                                code_col = "代码" if "代码" in df.columns else df.columns[0]
                                with index_lock:
                                    for _, row in df.iterrows():
                                        stock_code = str(row[code_col])[:6]
                                        if len(stock_code) >= 6:
                                            if stock_code not in _concept_index:
                                                _concept_index[stock_code] = []
                                            if cn not in _concept_index[stock_code]:
                                                _concept_index[stock_code].append(cn)
                                return 1
                        except Exception:
                            pass
                        return 0

                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        futures = {pool.submit(_fetch_one_em, cn): cn for cn in concept_names}
                        for future in as_completed(futures, timeout=120):
                            try:
                                scanned += future.result(timeout=30)
                            except Exception:
                                pass
            except Exception as e:
                logger.debug("东财概念板块接口失败: %s", e)

        logger.info("概念板块反查索引构建完成: %d 个概念, %d 只个股",
                    scanned, len(_concept_index))
    except Exception as e:
        logger.warning("概念板块反查索引构建失败: %s", e)

    return _concept_index


def fetch_stock_concepts(code: str, max_concepts: int = 5) -> List[str]:
    """
    获取个股所属概念板块（无单次 API 调用，纯本地反查）

    Args:
        code: 6 位股票代码
        max_concepts: 最多返回的概念板块数量

    Returns:
        [概念名称, ...]，查询失败返回空列表
    """
    if not code:
        return []
    index = _build_concept_index()
    concepts = index.get(code, [])
    return concepts[:max_concepts]


# fetch_stock_sw_industry_full 的 session 级内存缓存
# {code: {"result": dict, "ts": timestamp}}
# 同一次运行内不重复调 API（板块归属变化极慢）
_sw_industry_session_cache: Dict[str, Dict] = {}
_SW_INDUSTRY_CACHE_TTL_SECONDS = 3600  # 1 小时内不重复查同一只股票

# stock_individual_info_em session 级失败标记
# RemoteDisconnected 等反爬错误连续出现时，本 session 内不再调用
_individual_info_disabled: bool = False
_individual_info_fail_count: int = 0
_INDIVIDUAL_INFO_FAIL_THRESHOLD = 5  # 连续失败 5 次后短路


def _mark_individual_info_failure(reason: str):
    """记录 stock_individual_info_em 失败，连续失败超过阈值后短路"""
    global _individual_info_disabled, _individual_info_fail_count
    _individual_info_fail_count += 1
    if _individual_info_fail_count >= _INDIVIDUAL_INFO_FAIL_THRESHOLD and not _individual_info_disabled:
        _individual_info_disabled = True
        logger.warning(
            "stock_individual_info_em 连续失败 %d 次（最近: %s），本 session 内不再尝试，"
            "将仅依赖持久化缓存",
            _individual_info_fail_count, reason[:80],
        )


def _mark_individual_info_success():
    """成功时重置失败计数（不重置 _individual_info_disabled，避免反复触发）"""
    global _individual_info_fail_count
    _individual_info_fail_count = 0


def _sw_industry_cache_get(code: str) -> Optional[Dict[str, Optional[str]]]:
    """
    从 SQLite 读取持久化缓存的 SW 行业分类。

    板块归属关系变化极慢（股票的申万行业分类几乎不变），
    缓存 30 天有效。即使过期，API 失败时也返回旧缓存（标记 stale）。

    Returns:
        {"level1": ..., "level2": ..., "stale": bool} 或 None
    """
    try:
        from ..db import get_connection
        import json
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cache_value, expire_at FROM data_cache WHERE cache_key = ?",
            (f"sw_industry:{code}",),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            val = json.loads(row["cache_value"])
            # 检查是否过期（用于标记 stale）
            from datetime import datetime
            expire_at = datetime.fromisoformat(row["expire_at"].replace("Z", ""))
            val["stale"] = datetime.now() > expire_at
            return val
    except Exception as e:
        logger.debug("读取 SW 行业缓存失败 %s: %s", code, e)
    return None


def _sw_industry_cache_set(code: str, data: Dict[str, Optional[str]]):
    """
    写入 SQLite 持久化缓存。

    有效期 30 天（行业分类几乎不变）。
    仅缓存有实际值的记录（level1 或 level2 非空）。
    """
    if not (data.get("level1") or data.get("level2")):
        return  # 空结果不缓存
    try:
        from ..db import get_connection
        import json
        from datetime import datetime, timedelta
        conn = get_connection()
        cursor = conn.cursor()
        # 缓存数据（去掉 stale 标记）
        cache_data = {k: v for k, v in data.items() if k != "stale"}
        expiry = (datetime.now() + timedelta(days=30)).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO data_cache (cache_key, cache_value, expire_at) VALUES (?, ?, ?)",
            (f"sw_industry:{code}", json.dumps(cache_data, ensure_ascii=False), expiry),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("写入 SW 行业缓存失败 %s: %s", code, e)


def fetch_stock_sw_industry_full(code: str) -> Dict[str, Optional[str]]:
    """
    获取个股的完整申万行业分类（一级 + 二级）

    判定粒度：**优先用 SW 二级**（与新浪板块排名接口粒度一致，如"半导体"、"光伏设备"），
    level1 仅作为补充信息，不强制从 level2 反推（避免误判）。

    缓存策略（关键）：
      1. session 内存缓存（1 小时 TTL）— 同一次运行内不重复调 API
      2. SQLite 持久化缓存（30 天 TTL）— 跨进程复用，行业分类几乎不变
      3. API 失败时返回旧缓存（标记 stale=True），不阻塞主流程

    API 调用策略（缓存未命中时）：
      1. _build_sector_index 全局反查索引（SW API，失败自动短路）
      2. ak.stock_individual_info_em 个股信息（东财个股接口）
         - 连续失败 5 次后 session 级短路
         - 单次调用重试 2 次（RemoteDisconnected 是临时错误）

    Args:
        code: 6 位股票代码

    Returns:
        {"level1": "电子", "level2": "半导体", "stale": bool}
        stale=True 表示用的是过期缓存（API 失败时的兜底）
        查询完全失败返回 {"level1": None, "level2": None, "stale": False}
    """
    result = {"level1": None, "level2": None, "level3": None, "stale": False}
    if not code:
        return result

    import time

    # --- 缓存层 1: session 内存缓存（1 小时 TTL） ---
    now = time.time()
    cached = _sw_industry_session_cache.get(code)
    if cached and (now - cached["ts"] < _SW_INDUSTRY_CACHE_TTL_SECONDS):
        logger.debug("fetch_stock_sw_industry_full %s 命中 session 缓存", code)
        return cached["result"]

    # --- 缓存层 2: SQLite 持久化缓存（30 天 TTL，过期标记 stale） ---
    db_cached = _sw_industry_cache_get(code)
    if db_cached:
        is_stale = db_cached.get("stale", False)
        if not is_stale:
            # 未过期，直接返回
            result = {
                "level1": db_cached.get("level1"),
                "level2": db_cached.get("level2"),
                "level3": db_cached.get("level3"),
                "stale": False,
            }
            _sw_industry_session_cache[code] = {"result": result, "ts": now}
            logger.debug("fetch_stock_sw_industry_full %s 命中 SQLite 缓存（未过期）", code)
            return result
        # 过期了，继续走 API；若 API 失败再用旧缓存兜底

    # --- API 调用层 ---
    api_result = {"level1": None, "level2": None, "level3": None}

    try:
        import akshare as ak

        # --- 策略 1: 全局反查索引（拿 SW 一级代码） ---
        # SW API 失败时这里会快速返回空（session 级短路）
        idx = _build_sector_index()
        if idx and code in idx:
            sw_code = idx[code]
            sw_name = SW_LEVEL1.get(sw_code, "")
            api_result["level1"] = sw_name if sw_name else None

        # --- 策略 2: stock_individual_info_em 个股信息（拿东财行业名作为 level2） ---
        # 连续失败 5 次后短路，不再调用
        if not _individual_info_disabled:
            for attempt in range(2):  # 重试 2 次（RemoteDisconnected 是临时错误）
                try:
                    info_df = ak.stock_individual_info_em(symbol=code)
                    if info_df is None or info_df.empty:
                        logger.info("stock_individual_info_em %s 返回空（东财个股接口无数据）", code)
                    elif "item" not in info_df.columns:
                        logger.warning(
                            "stock_individual_info_em %s 列异常: %s",
                            code, list(info_df.columns),
                        )
                    else:
                        # 兼容多种行业字段名
                        for industry_col in ["行业", "所属行业", "东财行业"]:
                            industry_row = info_df[info_df["item"] == industry_col]
                            if not industry_row.empty:
                                ind_name = str(industry_row["value"].iloc[0])
                                if ind_name and ind_name not in ("nan", "--", "None", ""):
                                    api_result["level2"] = ind_name
                                    logger.info(
                                        "stock_individual_info_em %s 行业字段 '%s' = '%s'",
                                        code, industry_col, ind_name,
                                    )
                                    _mark_individual_info_success()
                                    break
                        else:
                            logger.info(
                                "stock_individual_info_em %s 未找到行业字段, item 列表: %s",
                                code, list(info_df["item"])[:10],
                            )
                    break  # 成功（或返回空），不再重试
                except Exception as e:
                    err_msg = str(e)[:120]
                    if "RemoteDisconnected" in err_msg or "Connection aborted" in err_msg:
                        # 反爬错误，重试一次
                        if attempt == 0:
                            logger.info(
                                "stock_individual_info_em %s 连接断开，0.5s 后重试: %s",
                                code, err_msg[:60],
                            )
                            time.sleep(0.5)
                            continue
                    # 重试仍失败或非临时错误
                    logger.warning(
                        "stock_individual_info_em %s 异常（尝试 %d/2）: %s",
                        code, attempt + 1, err_msg,
                    )
                    _mark_individual_info_failure(err_msg)
                    break
        else:
            logger.debug("stock_individual_info_em 已短路，跳过 %s", code)

        # --- 策略 3: 若 level2 为空但 level1 有值，用 level1 兜底 level2 ---
        if api_result.get("level1") and not api_result.get("level2"):
            api_result["level2"] = api_result["level1"]

        # --- 判断 API 是否成功 ---
        api_success = bool(api_result.get("level1") or api_result.get("level2"))

        if api_success:
            # API 成功 → 写入持久化缓存
            result = {**api_result, "stale": False}
            _sw_industry_cache_set(code, api_result)
            _sw_industry_session_cache[code] = {"result": result, "ts": now}
        elif db_cached:
            # API 失败但有旧缓存 → 返回旧缓存（标记 stale）
            result = {
                "level1": db_cached.get("level1"),
                "level2": db_cached.get("level2"),
                "level3": db_cached.get("level3"),
                "stale": True,
            }
            _sw_industry_session_cache[code] = {"result": result, "ts": now}
            logger.info(
                "fetch_stock_sw_industry_full %s API 失败，使用旧缓存（stale）: level2=%s",
                code, result.get("level2"),
            )
        else:
            # API 失败且无缓存 → 全部 None
            result = {**api_result, "stale": False}
            sw_status = "已短路" if _sw_api_disabled else f"失败{_sw_api_fail_count}次"
            em_status = "已短路" if _individual_info_disabled else f"失败{_individual_info_fail_count}次"
            logger.info(
                "fetch_stock_sw_industry_full %s 全部策略失败（无缓存兜底）: "
                "SW API %s, stock_individual_info_em %s",
                code, sw_status, em_status,
            )
    except Exception as e:
        logger.warning("获取 SW 完整行业分类失败 %s: %s", code, e)
        # 异常时也尝试用旧缓存兜底
        if db_cached:
            result = {
                "level1": db_cached.get("level1"),
                "level2": db_cached.get("level2"),
                "level3": db_cached.get("level3"),
                "stale": True,
            }
            logger.info("fetch_stock_sw_industry_full %s 异常，使用旧缓存（stale）", code)

    return result


# ---------------------------------------------------------------------------
# 概念板块趋势判定（替代 SW 一级行业判定）
# ---------------------------------------------------------------------------

# 概念板块状态缓存：{concept_name: {"ma_alignment": str, "above_ma20": bool}}
_concept_status_cache: Dict[str, Dict] = {}


def fetch_concept_status(concept_name: str) -> Dict:
    """
    获取概念板块的趋势状态（基于概念指数 K 线均线排列）

    使用 AKShare stock_board_concept_hist_em 获取概念指数日K线，
    计算 MA5/MA10/MA20 排列和 MA20 位置。

    Args:
        concept_name: 概念板块名称

    Returns:
        {"ma_alignment": "bullish"|"bearish"|"cross", "above_ma20": bool}
        获取失败返回空字典
    """
    if not concept_name:
        return {}

    if concept_name in _concept_status_cache:
        return _concept_status_cache[concept_name]

    result = {}
    try:
        import akshare as ak
        # 尝试获取概念板块历史K线（90天足够均线计算）
        df = None
        for func in [ak.stock_board_concept_hist_em, getattr(ak, 'stock_board_concept_hist_ths', None)]:
            if func is None:
                continue
            try:
                df = func(symbol=concept_name, period='daily', start_date='20260401', end_date='20260712', adjust='')
                if df is not None and not df.empty:
                    break
            except Exception:
                continue

        if df is not None and len(df) >= 20:
            # 提取收盘价
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
    """
    将概念板块均线状态映射为分类标签

    Args:
        ma_alignment: "bullish"|"bearish"|"cross"
        above_ma20: 是否站上MA20

    Returns:
        "主线"|"轮动"|"退潮"|"未知"
    """
    if ma_alignment == "bullish" and above_ma20:
        return "主线"
    elif ma_alignment == "bearish" and not above_ma20:
        return "退潮"
    elif ma_alignment:
        return "轮动"
    return "未知"
