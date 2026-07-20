"""
机构持仓打分模块

数据源（4 个）：
  1. 北向资金 — stock_hsgt_individual_em 个股北向持股明细
     判断：持股数环比变化（增/减/平）
  2. 龙虎榜机构席位 — stock_lhb_jgmmtj 龙虎榜机构买卖统计
     判断：机构净买入额（>0 看多）
  3. 主力资金流 — stock_individual_fund_flow 个股主力净流入
     判断：连续 3 日主力净流入为正
  4. 股东户数变化 — stock_zh_a_gdhs 股东户数
     判断：股东户数环比减少（筹码集中，看多）

打分逻辑：简单投票制
  每个数据源看多 +1 票，看空 -1 票，无数据 0 票
  累计 ≥2 票视为机构看多；≤-2 票视为机构看空

缓存策略：仅 session 内存缓存（用户选择）
  同一次运行内不重复调 API（1 小时 TTL）

降级策略：API 失败默认中性
  失败时该数据源 0 票，不影响其他数据源打分

权重设置（中等权重）：
  - 个股层面：占 vote_score 的 1/4（25%）
  - 板块层面：作为第 7 条件参与板块三级分类
"""
import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session 内存缓存
# {code: {"result": dict, "ts": timestamp}}
# 同一次运行内不重复调 API（1 小时 TTL）
# ---------------------------------------------------------------------------
_institutional_session_cache: Dict[str, Dict] = {}
_INSTITUTIONAL_CACHE_TTL = 3600  # 1 小时

# session 级失败标记（类似 _sw_api_disabled）
# 连续失败超过阈值后本 session 内不再调用对应 API
_api_disabled = {
    "north_bound": False,
    "lhb": False,
    "main_force": False,
    "shareholder": False,
}
_api_fail_count = {
    "north_bound": 0,
    "lhb": 0,
    "main_force": 0,
    "shareholder": 0,
}
_API_FAIL_THRESHOLD = 5  # 连续失败 5 次后短路


def _mark_api_failure(api_name: str, reason: str):
    """记录 API 失败，连续失败超过阈值后短路"""
    _api_fail_count[api_name] += 1
    if _api_fail_count[api_name] >= _API_FAIL_THRESHOLD and not _api_disabled[api_name]:
        _api_disabled[api_name] = True
        logger.warning(
            "机构持仓 API %s 连续失败 %d 次（最近: %s），本 session 内不再尝试",
            api_name, _api_fail_count[api_name], reason[:80],
        )


def _mark_api_success(api_name: str):
    """成功时重置失败计数"""
    _api_fail_count[api_name] = 0


def _reset_institutional_state():
    """重置所有状态（仅供测试用）"""
    global _api_disabled, _api_fail_count, _institutional_session_cache
    _api_disabled = {k: False for k in _api_disabled}
    _api_fail_count = {k: 0 for k in _api_fail_count}
    _institutional_session_cache.clear()


# ---------------------------------------------------------------------------
# 数据源 1: 北向资金（个股北向持股明细）
# ---------------------------------------------------------------------------

def _fetch_north_bound_holding(code: str) -> Dict[str, Any]:
    """
    查询个股北向持股明细，判断增持/减持。

    数据源：ak.stock_hsgt_individual_em(symbol=code)
    注意：参数名是 symbol（不是 stock）。

    返回近 30 日北向持股变化，取最新一日 vs 3 日前对比。

    Returns:
        {"vote": 1/-1/0, "detail": str, "raw": dict}
        vote: 1=增持看多, -1=减持看空, 0=无变化或无数据
    """
    if _api_disabled["north_bound"]:
        return {"vote": 0, "detail": "北向接口已短路", "raw": {}}

    try:
        import akshare as ak
        # ⚠️ 参数名是 symbol，不是 stock（akshare 源码确认）
        # 某些股票（如 688652）东财返回 result=None（"返回数据为空"），
        # akshare 内部没处理 None 直接 subscript 报 'NoneType' object is not subscriptable
        # 用 try-catch 包住，当作无数据处理
        try:
            df = ak.stock_hsgt_individual_em(symbol=code)
        except (TypeError, KeyError, IndexError) as e:
            # akshare 内部异常（'NoneType' object is not subscriptable 等）
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": "北向无数据(东财返回空)", "raw": {}}
        if df is None or df.empty:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": "无北向持股数据", "raw": {}}

        # 兼容列名（akshare 真实列名：持股日期、持股数量）
        date_col = "持股日期" if "持股日期" in df.columns else (
            "日期" if "日期" in df.columns else df.columns[0]
        )
        hold_col = None
        for c in ["持股数量", "持股股数", "持股数"]:
            if c in df.columns:
                hold_col = c
                break
        if not hold_col:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": f"北向列名异常: {list(df.columns)[:8]}", "raw": {}}

        # 按日期排序，取最近 4 个交易日
        df = df.sort_values(date_col, ascending=False).head(4)
        if len(df) < 2:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": "北向数据不足 2 日", "raw": {}}

        latest_hold = float(df.iloc[0][hold_col])
        prev_hold = float(df.iloc[-1][hold_col])

        if prev_hold <= 0:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": "前期无持股，无法判断趋势", "raw": {}}

        change_pct = (latest_hold - prev_hold) / prev_hold
        _mark_api_success("north_bound")

        if change_pct > 0.01:  # 增持 >1%
            return {
                "vote": 1,
                "detail": f"北向增持 {change_pct*100:.1f}%（{prev_hold:.0f}→{latest_hold:.0f}）",
                "raw": {"latest": latest_hold, "prev": prev_hold, "change_pct": change_pct},
            }
        elif change_pct < -0.01:  # 减持 >1%
            return {
                "vote": -1,
                "detail": f"北向减持 {change_pct*100:.1f}%（{prev_hold:.0f}→{latest_hold:.0f}）",
                "raw": {"latest": latest_hold, "prev": prev_hold, "change_pct": change_pct},
            }
        else:
            return {
                "vote": 0,
                "detail": f"北向持股持平（变化 {change_pct*100:.1f}%）",
                "raw": {"latest": latest_hold, "prev": prev_hold, "change_pct": change_pct},
            }
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("north_bound", err_msg)
        return {"vote": 0, "detail": f"北向接口异常: {err_msg}", "raw": {}}


# ---------------------------------------------------------------------------
# 数据源 2: 龙虎榜机构席位
# ---------------------------------------------------------------------------

# 全市场龙虎榜数据 session 级缓存（stock_lhb_detail_em 返回全市场数据）
# 当日内有效，17 只股票共享一份缓存，tqdm 只触发一次
_lhb_market_cache: Optional[Any] = None
_lhb_market_cache_date: str = ""


def _get_lhb_market_data():
    """
    获取全市场龙虎榜明细（session 级缓存，当日内有效）。

    ak.stock_lhb_detail_em(start_date, end_date) 返回日期范围内的全市场龙虎榜，
    akshare 内部用 tqdm 遍历分页（约 10-30 页）。
    如果每只股票都调一次，17 只 = 17 次遍历。

    优化：session 内只拉一次（近 30 日），后续从缓存 DataFrame 过滤该股票。
    """
    global _lhb_market_cache, _lhb_market_cache_date
    today = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    if _lhb_market_cache is not None and _lhb_market_cache_date == today:
        return _lhb_market_cache

    try:
        import akshare as ak
        # ⚠️ stock_lhb_detail_em 不接受 symbol 参数，只接受 start_date/end_date
        # 返回全市场龙虎榜明细，包含 BILLBOARD_NET_AMT（净额）等字段
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=today)
        _lhb_market_cache = df
        _lhb_market_cache_date = today
        if df is not None and not df.empty:
            logger.info("全市场龙虎榜数据已缓存: %d 条（近30日，当日有效）", len(df))
        return df
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("lhb", err_msg)
        return None


def _fetch_lhb_institutional(code: str) -> Dict[str, Any]:
    """
    查询个股近期龙虎榜记录，判断机构净买卖。

    数据源：ak.stock_lhb_detail_em(start_date, end_date) 全市场数据（session 缓存）
    然后过滤出该股票的记录。

    ⚠️ stock_lhb_detail_em 不接受 symbol 参数，只接受日期范围。
    返回全市场数据后按 SECURITY_CODE/代码 列过滤。

    Returns:
        {"vote": 1/-1/0, "detail": str, "raw": dict}
        vote: 1=净买入看多, -1=净卖出看空, 0=无龙虎榜数据
    """
    if _api_disabled["lhb"]:
        return {"vote": 0, "detail": "龙虎榜接口已短路", "raw": {}}

    df = _get_lhb_market_data()
    if df is None or df.empty:
        return {"vote": 0, "detail": "无龙虎榜数据", "raw": {}}

    _mark_api_success("lhb")

    try:
        # 兼容列名
        code_col = None
        for c in ["代码", "股票代码", "SECURITY_CODE"]:
            if c in df.columns:
                code_col = c
                break
        if not code_col:
            return {"vote": 0, "detail": f"龙虎榜列名异常: {list(df.columns)[:8]}", "raw": {}}

        # 过滤该股票
        stock_rows = df[df[code_col].astype(str) == code]
        if stock_rows.empty:
            return {"vote": 0, "detail": "近30日无龙虎榜记录", "raw": {}}

        # 找净买入列（akshare 真实列名：龙虎榜净买额）
        net_col = None
        for c in ["龙虎榜净买额", "龙虎榜净额", "净额", "BILLBOARD_NET_AMT"]:
            if c in stock_rows.columns:
                net_col = c
                break
        if not net_col:
            return {"vote": 0, "detail": f"龙虎榜净额列异常: {list(stock_rows.columns)[:8]}", "raw": {}}

        # 汇总该股票近30日所有龙虎榜净额
        total_net = float(stock_rows[net_col].sum())
        appear_count = len(stock_rows)

        if total_net > 50000000:  # 净买入 > 5000 万
            return {
                "vote": 1,
                "detail": f"龙虎榜净买入 {total_net/1e8:.2f} 亿（{appear_count} 次上榜）",
                "raw": {"total_net": total_net, "appear_count": appear_count},
            }
        elif total_net < -50000000:  # 净卖出 > 5000 万
            return {
                "vote": -1,
                "detail": f"龙虎榜净卖出 {total_net/1e8:.2f} 亿（{appear_count} 次上榜）",
                "raw": {"total_net": total_net, "appear_count": appear_count},
            }
        else:
            return {
                "vote": 0,
                "detail": f"龙虎榜净额 {total_net/1e4:.0f} 万（{appear_count} 次上榜，未达阈值）",
                "raw": {"total_net": total_net, "appear_count": appear_count},
            }
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("lhb", err_msg)
        return {"vote": 0, "detail": f"龙虎榜数据处理异常: {err_msg}", "raw": {}}


# ---------------------------------------------------------------------------
# 数据源 3: 主力资金流（连续 3 日净流入）
# ---------------------------------------------------------------------------

def _fetch_main_force_flow(code: str) -> Dict[str, Any]:
    """
    查询个股主力资金流，判断连续 3 日净流入。

    数据源：直接 HTTP 请求东财 push2his（绕过 akshare 封装）
    URL: https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get

    push2his 偶发反爬（RemoteDisconnected），带 8 次重试，每次间隔 2s。
    返回 K 线格式：日期,主力净流入,小单净流入,中单净流入,大单净流入,超大单净流入,...

    Returns:
        {"vote": 1/-1/0, "detail": str, "raw": dict}
        vote: 1=连续 3 日主力净流入看多, -1=连续 3 日净流出看空, 0=无趋势
    """
    if _api_disabled["main_force"]:
        return {"vote": 0, "detail": "主力资金接口已短路", "raw": {}}

    try:
        import requests
        import time as _time

        # 判断市场前缀（secid 格式：1.代码=沪，0.代码=深/北）
        if code.startswith("6"):
            secid = f"1.{code}"
        else:
            secid = f"0.{code}"

        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {
            "lmt": "0",
            "klt": "101",  # 日 K
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0",
            "Referer": "https://data.eastmoney.com/",
        }

        data = None
        for attempt in range(8):  # 8 次重试
            try:
                r = requests.get(url, params=params, timeout=12, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("data") and data["data"].get("klines"):
                        break
                _time.sleep(2)
            except Exception:
                _time.sleep(2)

        if not data or not data.get("data") or not data["data"].get("klines"):
            _mark_api_failure("main_force", "无数据或重试耗尽")
            return {"vote": 0, "detail": "主力资金接口异常(重试耗尽)", "raw": {}}

        # 解析 K 线：日期,主力净流入,小单,中单,大单,超大单,...
        klines = data["data"]["klines"]
        if len(klines) < 3:
            _mark_api_success("main_force")
            return {"vote": 0, "detail": "主力资金数据不足 3 日", "raw": {}}

        # 取最近 3 日的主力净流入（第 2 列，索引 1）
        net_flows = []
        for kline in klines[-3:]:
            parts = kline.split(",")
            if len(parts) >= 2:
                try:
                    net_flows.append(float(parts[1]))  # 主力净流入-净额
                except (ValueError, IndexError):
                    net_flows.append(0.0)

        if len(net_flows) < 3:
            _mark_api_success("main_force")
            return {"vote": 0, "detail": "主力资金解析不足 3 日", "raw": {}}

        _mark_api_success("main_force")

        # 连续 3 日净流入
        if all(f > 0 for f in net_flows):
            total = sum(net_flows)
            return {
                "vote": 1,
                "detail": f"主力连续 3 日净流入（合计 {total/1e8:.2f} 亿）",
                "raw": {"net_flows": net_flows, "total": total},
            }
        # 连续 3 日净流出
        elif all(f < 0 for f in net_flows):
            total = sum(net_flows)
            return {
                "vote": -1,
                "detail": f"主力连续 3 日净流出（合计 {total/1e8:.2f} 亿）",
                "raw": {"net_flows": net_flows, "total": total},
            }
        else:
            return {
                "vote": 0,
                "detail": f"主力资金流向不一（3 日: {[round(f/1e4,0) for f in net_flows]}万）",
                "raw": {"net_flows": net_flows},
            }
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("main_force", err_msg)
        return {"vote": 0, "detail": f"主力资金接口异常: {err_msg}", "raw": {}}


# ---------------------------------------------------------------------------
# 数据源 4: 股东户数变化
# ---------------------------------------------------------------------------

def _fetch_shareholder_count(code: str) -> Dict[str, Any]:
    """
    查询个股股东户数变化，判断筹码集中度。

    数据源：ak.stock_zh_a_gdhs_detail_em(symbol=code)
    这是**单只股票**的股东户数查询。

    ⚠️ 不要用 ak.stock_zh_a_gdhs —— 它的 symbol 参数是**日期**（如 "20230930"），
    返回全市场所有股票的股东户数，akshare 内部用 tqdm 遍历 ~860 页，单次调用约 30 秒。

    对比最近两期股东户数，减少视为筹码集中（看多）。

    Returns:
        {"vote": 1/-1/0, "detail": str, "raw": dict}
        vote: 1=户数减少筹码集中看多, -1=户数增加筹码分散看空, 0=无变化或无数据
    """
    if _api_disabled["shareholder"]:
        return {"vote": 0, "detail": "股东户数接口已短路", "raw": {}}

    try:
        import akshare as ak
        # 单股查询（注意是 stock_zh_a_gdhs_detail_em，不是 stock_zh_a_gdhs）
        df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if df is None or df.empty:
            _mark_api_success("shareholder")
            return {"vote": 0, "detail": "无股东户数数据", "raw": {}}

        # 兼容列名（akshare 真实列名：股东户数统计截止日、股东户数-本次、股东户数-上次）
        date_col = "股东户数统计截止日" if "股东户数统计截止日" in df.columns else (
            "截止日期" if "截止日期" in df.columns else (
                "END_DATE" if "END_DATE" in df.columns else df.columns[0]
            )
        )
        # 优先用"股东户数-本次"和"股东户数-上次"（akshare 真实列名）
        latest_count_col = None
        prev_count_col = None
        for c in ["股东户数-本次", "股东户数", "户数", "HOLDER_NUM"]:
            if c in df.columns:
                latest_count_col = c
                break
        for c in ["股东户数-上次", "PRE_HOLDER_NUM"]:
            if c in df.columns:
                prev_count_col = c
                break
        if not latest_count_col:
            _mark_api_success("shareholder")
            return {"vote": 0, "detail": f"股东户数列名异常: {list(df.columns)[:8]}", "raw": {}}

        # 按日期排序，取最近 2 期
        df = df.sort_values(date_col, ascending=False).head(2)
        if len(df) < 2:
            _mark_api_success("shareholder")
            return {"vote": 0, "detail": "股东户数数据不足 2 期", "raw": {}}

        latest_count = float(df.iloc[0][latest_count_col])
        # 优先用"股东户数-上次"列，否则用第二行的"股东户数-本次"
        if prev_count_col:
            prev_count = float(df.iloc[0][prev_count_col])
        else:
            prev_count = float(df.iloc[1][latest_count_col])

        if prev_count <= 0:
            _mark_api_success("shareholder")
            return {"vote": 0, "detail": "前期户数异常", "raw": {}}

        change_pct = (latest_count - prev_count) / prev_count
        _mark_api_success("shareholder")

        if change_pct < -0.02:  # 户数减少 >2%（筹码集中）
            return {
                "vote": 1,
                "detail": f"股东户数减少 {change_pct*100:.1f}%（{prev_count:.0f}→{latest_count:.0f}，筹码集中）",
                "raw": {"latest": latest_count, "prev": prev_count, "change_pct": change_pct},
            }
        elif change_pct > 0.02:  # 户数增加 >2%（筹码分散）
            return {
                "vote": -1,
                "detail": f"股东户数增加 {change_pct*100:.1f}%（{prev_count:.0f}→{latest_count:.0f}，筹码分散）",
                "raw": {"latest": latest_count, "prev": prev_count, "change_pct": change_pct},
            }
        else:
            return {
                "vote": 0,
                "detail": f"股东户数持平（变化 {change_pct*100:.1f}%）",
                "raw": {"latest": latest_count, "prev": prev_count, "change_pct": change_pct},
            }
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("shareholder", err_msg)
        return {"vote": 0, "detail": f"股东户数接口异常: {err_msg}", "raw": {}}


# ---------------------------------------------------------------------------
# 主入口：综合打分
# ---------------------------------------------------------------------------

def score_institutional_holding(code: str) -> Dict[str, Any]:
    """
    机构持仓综合打分（简单投票制）。

    调用 4 个数据源，每个数据源投 1/-1/0 票，累计得到总分。
    缓存策略：session 内存缓存（1 小时 TTL），同一次运行内不重复调 API。
    降级策略：API 失败默认 0 票（中性），不影响其他数据源。

    Args:
        code: 6 位股票代码

    Returns:
        {
            "vote_score": int,          # 总票数（-4 到 +4）
            "vote_label": str,          # "机构看多"/"机构看空"/"机构中性"
            "votes": {                  # 各数据源投票详情
                "north_bound": {"vote": 1, "detail": "..."},
                "lhb": {"vote": -1, "detail": "..."},
                "main_force": {"vote": 1, "detail": "..."},
                "shareholder": {"vote": 0, "detail": "..."},
            },
            "bullish_count": int,       # 看多票数
            "bearish_count": int,       # 看空票数
            "neutral_count": int,       # 中性票数
            "stale": bool,              # 是否使用缓存（True=缓存命中）
        }
    """
    # session 缓存检查
    now = time.time()
    cached = _institutional_session_cache.get(code)
    if cached and (now - cached["ts"] < _INSTITUTIONAL_CACHE_TTL):
        cached_result = dict(cached["result"])
        cached_result["stale"] = True
        return cached_result

    # 调用 4 个数据源
    votes = {
        "north_bound": _fetch_north_bound_holding(code),
        "lhb": _fetch_lhb_institutional(code),
        "main_force": _fetch_main_force_flow(code),
        "shareholder": _fetch_shareholder_count(code),
    }

    vote_scores = [v["vote"] for v in votes.values()]
    total_score = sum(vote_scores)
    bullish_count = sum(1 for v in vote_scores if v > 0)
    bearish_count = sum(1 for v in vote_scores if v < 0)
    neutral_count = sum(1 for v in vote_scores if v == 0)

    # 标签
    if total_score >= 2:
        vote_label = "机构看多"
    elif total_score <= -2:
        vote_label = "机构看空"
    elif total_score > 0:
        vote_label = "机构偏多"
    elif total_score < 0:
        vote_label = "机构偏空"
    else:
        vote_label = "机构中性"

    result = {
        "vote_score": total_score,
        "vote_label": vote_label,
        "votes": votes,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "stale": False,
    }

    # 写入 session 缓存
    _institutional_session_cache[code] = {"result": result, "ts": now}

    logger.info(
        "机构持仓打分 %s: 总分=%d (%s), 看多=%d/看空=%d/中性=%d",
        code, total_score, vote_label, bullish_count, bearish_count, neutral_count,
    )

    return result


def score_institutional_for_sector(stock_codes: List[str]) -> Dict[str, Any]:
    """
    批量机构持仓打分：统计一组股票中机构净买入股票数。

    ⚠️ 警告：此函数会遍历每个股票调用 4 个 API，仅适用于**少量持仓股**（如 10-20 只）。
    **绝对不要**用于板块成分股遍历（30 只 × 4 API = 120 次调用会触发反爬）。

    板块层面的机构资金判断请使用 sector_data['real_fund_flow']（已有，无需额外 API）。

    Args:
        stock_codes: 股票代码列表（建议 ≤ 20 只）

    Returns:
        {
            "institutional_bullish_count": int,   # 机构看多股票数
            "institutional_bearish_count": int,   # 机构看空股票数
            "institutional_net_bullish": int,     # 净看多股票数
            "institutional_score": float,         # 归一化得分 0-1（看多占比）
            "detail": str,                        # 描述
        }
    """
    if not stock_codes:
        return {
            "institutional_bullish_count": 0,
            "institutional_bearish_count": 0,
            "institutional_net_bullish": 0,
            "institutional_score": 0.0,
            "detail": "板块无股票",
        }

    bullish = 0
    bearish = 0
    for code in stock_codes:
        result = score_institutional_holding(code)
        if result["vote_score"] >= 2:
            bullish += 1
        elif result["vote_score"] <= -2:
            bearish += 1

    net_bullish = bullish - bearish
    score = bullish / len(stock_codes) if stock_codes else 0.0

    detail = f"板块内机构看多 {bullish} 只, 看空 {bearish} 只, 净看多 {net_bullish} 只"

    logger.info(
        "板块机构持仓打分: %d 只股票, 看多=%d, 看空=%d, 净看多=%d, 得分=%.2f",
        len(stock_codes), bullish, bearish, net_bullish, score,
    )

    return {
        "institutional_bullish_count": bullish,
        "institutional_bearish_count": bearish,
        "institutional_net_bullish": net_bullish,
        "institutional_score": score,
        "detail": detail,
    }
