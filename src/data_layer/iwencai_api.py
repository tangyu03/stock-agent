"""
问财 OpenAPI 客户端
统一的问财数据查询入口，替代 AKShare 爬虫方案，走正规 API 通道。

使用方式:
    from .iwencai_api import query_iwencai, query_industry_fund_flow, query_stock_fund_flow

环境变量:
    IWENCAI_API_KEY    问财 API 密钥（必填）
    IWENCAI_BASE_URL   API 网关地址（默认 https://openapi.iwencai.com）
"""

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
IWENCAI_BASE_URL = os.environ.get("IWENCAI_BASE_URL", "https://openapi.iwencai.com")
IWENCAI_API_KEY = os.environ.get("IWENCAI_API_KEY", "")
API_ENDPOINT = f"{IWENCAI_BASE_URL}/v1/query2data"
SKILL_ID = "hithink-market-query"
SKILL_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 15  # 秒

# P1-2 审计（2026-08-18）：无 key 只告警一次，避免每次调用都刷屏
_no_key_warned = False


# ---------------------------------------------------------------------------
# 底层 HTTP 调用
# ---------------------------------------------------------------------------
def _build_headers(call_type: str = "normal") -> Dict[str, str]:
    """构造问财网关规范的请求头。"""
    trace_id = secrets.token_hex(32)  # 64 字符唯一 ID
    return {
        "Authorization": f"Bearer {IWENCAI_API_KEY}",
        "Content-Type": "application/json",
        "X-Claw-Call-Type": call_type,
        "X-Claw-Skill-Id": SKILL_ID,
        "X-Claw-Skill-Version": SKILL_VERSION,
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": trace_id,
    }


def _call_api(
    query: str,
    page: str = "1",
    limit: str = "50",
    call_type: str = "normal",
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """
    调用问财 OpenAPI 查询数据。

    Args:
        query: 自然语言查询问句
        page: 分页参数
        limit: 每页条数（默认 50，比默认 10 大，减少翻页）
        call_type: normal 或 retry
        timeout: 超时秒数

    Returns:
        {"datas": [...], "code_count": int, ...} 或 None（失败时）
    """
    global _no_key_warned
    if not IWENCAI_API_KEY:
        if not _no_key_warned:
            _no_key_warned = True
            logger.warning("IWENCAI_API_KEY 未设置，跳过问财 API 调用（仅提示一次）")
        return None

    payload = {
        "query": query,
        "page": page,
        "limit": limit,
        "is_cache": "1",
        "expand_index": "true",
    }
    headers = _build_headers(call_type)

    try:
        req = urllib.request.Request(
            API_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body.strip():
                return None
            return json.loads(body)
    except urllib.error.HTTPError as e:
        logger.warning("问财 API HTTP %s: %s", e.code, e.reason)
        return None
    except urllib.error.URLError as e:
        logger.warning("问财 API 网络错误: %s", e.reason)
        return None
    except Exception as e:
        logger.warning("问财 API 调用失败: %s", str(e)[:120])
        return None


# ---------------------------------------------------------------------------
# 行业资金流查询（替代 ak.stock_fund_flow_industry）
# ---------------------------------------------------------------------------
# 缓存：同一天内只查一次
_industry_fund_cache: Optional[List[Dict]] = None
_industry_fund_cache_date: Optional[str] = None


def query_industry_fund_flow(force_refresh: bool = False) -> Optional[List[Dict]]:
    """
    获取全行业综合数据（当日缓存，一次调用覆盖所有维度）。

    查询字段：涨跌幅(当日/3日/5日)、主力资金流、MA均线值(5/10/20)、
    涨停家数、成交额、成分股数量。

    替代：K线计算(sector_change_3d/5d/MA)、AKShare 涨停池、成分股查询。

    Returns:
        [
            {
                "行业": "半导体", "THS代码": "881121",
                "最新涨跌幅:前复权": -6.39,   # 当日涨跌幅(%)
                "涨跌幅[5日区间]": -24.59,    # 5日涨跌幅(%)
                "涨跌幅[3日区间]": -20.11,    # 3日涨跌幅(%)
                "主力净流入": 9438459000.0,   # 资金净流入(元)
                "ma5": 17461.47,             # MA5 值
                "ma10": 19214.74,            # MA10 值
                "ma20": 20248.79,            # MA20 值
                "涨停家数": 0,               # 当日涨停股数
                "成交额": 441713440000.0,     # 当日成交额(元)
                "成份股数量": 181,            # 板块成分股数
            },
            ...
        ]
        或 None
    """
    global _industry_fund_cache, _industry_fund_cache_date

    today = time.strftime("%Y-%m-%d")

    if not force_refresh and _industry_fund_cache is not None and _industry_fund_cache_date == today:
        return _industry_fund_cache

    try:
        # 合并查询：一次获取所有行业维度
        result = _call_api(
            query="行业资金流向排名 最新涨跌幅 涨停家数 成份股数量",
            page="1",
            limit="66",  # 足够覆盖所有行业（实际约50-66个有资金流数据的）
            timeout=DEFAULT_TIMEOUT,
        )
        if not result:
            logger.warning("问财行业综合查询返回空")
            return None

        datas = result.get("datas", [])
        if not datas:
            logger.warning("问财行业综合查询 datas 为空")
            return None

        # 标准化：只提取核心稳定字段
        # 涨跌幅(3d/5d)和MA均线从K线计算（问财组合查询不稳定）
        normalized = []
        for item in datas:
            row = {}

            # 行业名
            for key in ("指数简称", "行业名称", "板块名称"):
                if key in item and item[key]:
                    row["行业"] = str(item[key])
                    break
            if "行业" not in row:
                continue

            # THS 代码（去后缀）
            code = item.get("指数代码", "")
            if code:
                row["THS代码"] = str(code).split(".")[0]

            # 涨跌幅(当日)
            v = item.get("最新涨跌幅:前复权")
            if v is not None:
                row["最新涨跌幅:前复权"] = _try_parse_number(v)

            # 主力净流入（匹配 "资金净流入额[日期]"）
            for k, v in item.items():
                if v is not None and "资金净流入额" in str(k):
                    row["主力净流入"] = _try_parse_number(v)
                    break

            # 涨停家数
            for k, v in item.items():
                if v is not None and "涨停家数" in str(k):
                    row["涨停家数"] = _try_parse_number(v)
                    break

            # 成分股数量
            if "成份股数量" in item:
                row["成份股数量"] = _try_parse_number(item["成份股数量"])

            normalized.append(row)

        logger.info("问财行业数据: %d 条 (涨跌幅/资金流/涨停/成分股)", len(normalized))
        _industry_fund_cache = normalized
        _industry_fund_cache_date = today
        return normalized

    except Exception as e:
        logger.warning("问财行业综合查询异常: %s", str(e)[:120])
        return None


# ---------------------------------------------------------------------------
# 个股资金流查询（替代 ak.stock_individual_fund_flow）
# ---------------------------------------------------------------------------
def query_stock_fund_flow(
    code: str, name: str = "", call_type: str = "normal"
) -> Optional[Dict[str, Any]]:
    """
    获取个股综合行情数据（主力资金 + 技术指标 + 盘口）。

    一次查询覆盖：主力净流入、MACD、KDJ、RSI、换手率、量比、振幅、成交量。

    Args:
        code: 6位股票代码
        name: 股票简称（可选）

    Returns:
        {
            "main_net": float,              # 主力净流入(元)
            "net_flows_3d": [float x3],      # 近3日主力净流入
            "net_flows_5d": [float x5],      # 近5日主力净流入
            "super_large_net": float,        # 当日超大单净流入
            "large_net": float,              # 当日大单净流入
            "super_large_flows_5d": [float], # 近5日超大单净流入
            "large_flows_5d": [float],       # 近5日大单净流入
            "signal": "流入"|"流出"|"平衡",
            "macd": float, "kdj": float, "rsi": float,
            "turnover_rate": float, "volume_ratio": float,
            "amplitude": float, "volume": float, "amount": float,
            "change_pct": float, "price": float,
        }
        或 None
    """
    query_text = f"{name or code} 近5日主力净流入 超大单净流入 大单净流入 MACD KDJ RSI 换手率 量比 振幅"
    if name:
        query_text = f"{name} 近5日主力净流入 超大单净流入 大单净流入 MACD KDJ RSI 换手率 量比 振幅"

    try:
        result = _call_api(
            query=query_text, page="1", limit="1", timeout=10, call_type=call_type
        )
        if not result:
            return None

        datas = result.get("datas", [])
        if not datas:
            return None

        item = datas[0]
        out = {}

        out["net_flows_5d"] = _extract_dated_metric_series(
            item, ("主力资金流向", "主力净流入", "主力资金净流入")
        )
        out["fund_flow_points_5d"] = _extract_dated_metric_points(
            item, ("主力资金流向", "主力净流入", "主力资金净流入")
        )
        out["net_flows_3d"] = out["net_flows_5d"][-3:]
        out["super_large_flows_5d"] = _extract_dated_metric_series(
            item, ("超大单净流入", "超大单资金流向", "超大单净额")
        )
        out["large_flows_5d"] = _extract_dated_metric_series(
            item, ("大单净流入", "大单资金流向", "大单净额")
        )

        # 最近1日主力净流入
        if out.get("net_flows_5d"):
            out["main_net"] = out["net_flows_5d"][-1]
            out["signal"] = "流入" if out["main_net"] > 0 else ("流出" if out["main_net"] < 0 else "平衡")
        else:
            # 降级：匹配无日期后缀的 "主力资金流向"
            main_net = _find_value(item, ["主力资金流向", "主力净流入", "主力资金净流入"])
            if main_net is not None:
                out["main_net"] = main_net
                out["signal"] = "流入" if main_net > 0 else ("流出" if main_net < 0 else "平衡")

        if out.get("super_large_flows_5d"):
            out["super_large_net"] = out["super_large_flows_5d"][-1]
        else:
            super_large = _find_value(item, ["超大单净流入", "超大单资金流向", "超大单净额"])
            if super_large is not None:
                out["super_large_net"] = super_large

        if out.get("large_flows_5d"):
            out["large_net"] = out["large_flows_5d"][-1]
        else:
            large = _find_value(item, ["大单净流入", "大单资金流向", "大单净额"])
            if large is not None:
                out["large_net"] = large

        if "main_net" not in out:
            return None

        # 技术指标
        for k, v in item.items():
            if v is None:
                continue
            if "macd" in str(k).lower():
                out["macd"] = _try_parse_number(v)
            elif "kdj" in str(k).lower():
                out["kdj"] = _try_parse_number(v)
            elif "rsi" in str(k).lower():
                out["rsi"] = _try_parse_number(v)
            elif "换手率" in str(k):
                out["turnover_rate"] = _try_parse_number(v)
            elif "量比" == str(k):
                out["volume_ratio"] = _try_parse_number(v)
            elif "振幅" in str(k):
                out["amplitude"] = _try_parse_number(v)
            elif "成交量" in str(k):
                out["volume"] = _try_parse_number(v)
            elif "成交额" in str(k):
                out["amount"] = _try_parse_number(v)
            elif "最新涨跌幅" == str(k):
                out["change_pct"] = _try_parse_number(v)
            elif "最新价" == str(k):
                out["price"] = _try_parse_number(v)

        return out
    except Exception as e:
        logger.debug("问财个股综合查询失败 %s: %s", code, str(e)[:80])
        return None


def batch_query_stock_fund_flow(
    codes_and_names: List[tuple],
    batch_size: int = 10,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    批量查询个股资金流。

    问财支持自然语言一句查多只，例如 "银行板块个股主力资金流向"。
    这里逐只查询，但用 batch 查询减少 API 调用次数。

    Args:
        codes_and_names: [(code, name), ...]
        batch_size: 每次查询的股票数量

    Returns:
        {code: fund_flow_dict or None}
    """
    results: Dict[str, Optional[Dict]] = {}
    total = len(codes_and_names)

    for i in range(0, total, batch_size):
        batch = codes_and_names[i:i + batch_size]

        # 构建批量查询：列出股票代码
        if len(batch) == 1:
            code, name = batch[0]
            results[code] = query_stock_fund_flow(code, name)
        else:
            # 多个股票：逐只查（问财对批量资金流支持有限）
            for code, name in batch:
                results[code] = query_stock_fund_flow(code, name)
                time.sleep(0.3)  # 请求间隔

        # 进度日志
        if total > 20 and (i + batch_size) % 50 == 0:
            done = min(i + batch_size, total)
            logger.info("批量个股资金流: %d/%d", done, total)

    hit = sum(1 for v in results.values() if v is not None)
    logger.info("批量个股资金流完成: %d/%d 命中 (问财API)", hit, total)
    return results


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _try_parse_number(val: Any) -> Any:
    """尝试将字符串转为数字，失败则返回原值。"""
    if not isinstance(val, str):
        return val
    # 去掉 % 、，逗号等
    cleaned = val.replace(",", "").replace("，", "").replace("%", "").replace("元", "").strip()
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except (ValueError, TypeError):
        return val


def _find_value(item: Dict, candidate_keys: List[str]) -> Optional[float]:
    """在返回数据中按优先级查找字段值，返回 float 或 None。"""
    for key in candidate_keys:
        if key in item:
            val = item[key]
            if val is None or val == "" or val == "-" or val == "--":
                continue
            try:
                if isinstance(val, str):
                    val = val.replace(",", "").replace("，", "").replace("元", "").strip()
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


def _extract_dated_metric_points(item: Dict, phrases: tuple) -> List[Dict[str, Any]]:
    """按日期字段提取指标序列，例如“主力资金流向[20260901]”。"""
    values: Dict[str, float] = {}
    for key, value in item.items():
        text = str(key)
        if "[" not in text or "]" not in text:
            continue
        metric = text.split("[", 1)[0]
        date = text.rsplit("[", 1)[1].rstrip("]")
        if not date or not any(phrase in metric for phrase in phrases):
            continue
        number = _find_value({metric: value}, [metric])
        if number is not None:
            values[date] = number
    return [
        {"date": date, "value": values[date]} for date in sorted(values)
    ][-5:]


def _extract_dated_metric_series(item: Dict, phrases: tuple) -> List[float]:
    return [
        point["value"]
        for point in _extract_dated_metric_points(item, phrases)
    ]
