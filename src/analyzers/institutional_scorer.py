"""
机构持仓打分模块

数据源（4 个）：
  1. 融资余额 — stock_margin_detail_sse/szse 融资融券明细（F9替换北向，2024-08起北向停披）
     判断：融资余额环比变化（增/减/平，>2%阈值）
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

from ..data_layer.akshare_safe import call_ak_with_retry  # P2-5: akshare 调用带超时+重试


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

# 主力资金流只按单股熔断。问财/备源都拿不到一只股票的数据时，
# 不应该让下一只股票直接放弃尝试。
_main_force_failures: Dict[str, int] = {}
_MAIN_FORCE_FAILURE_THRESHOLD = 1
_fund_flow_rank_cache: Dict[str, float] = {}
_fund_flow_rank_cache_at = 0.0
_FUND_FLOW_RANK_TTL = 15 * 60
_MAIN_FORCE_FALLBACK_BACKOFF_STEPS = (30, 60, 120, 300)
_main_force_fallback_failures = 0
_main_force_fallback_block_until = 0.0
_fund_flow_rank_source = ""
_fund_flow_rank_last_error = "尚未加载"
_FUND_FLOW_RANK_NET_COLUMNS = (
    "3日主力净流入-净额",
    "3日主力净流入净额",
    "3日主力净流入",
)
_detailed_fund_flow_cache: Dict[str, Any] = {}
_detailed_fund_flow_cache_at = 0.0
_DETAILED_FUND_FLOW_TTL = 5 * 60
_detailed_fund_flow_failures = 0
_detailed_fund_flow_block_until = 0.0
_DETAILED_FUND_FLOW_BACKOFF_STEPS = (30, 60, 120, 300)
_top10_holder_cache: Dict[str, Any] = {}
_top10_holder_cache_date = ""
_TOP10_HOLDER_TTL_DAYS = 1


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


def _mark_main_force_failure(code: str, reason: str):
    """单股资金流连续无数据后短路，避免同一股票反复拖慢流程"""
    clean_code = str(code).zfill(6)
    count = _main_force_failures.get(clean_code, 0) + 1
    _main_force_failures[clean_code] = count
    if count >= _MAIN_FORCE_FAILURE_THRESHOLD:
        logger.warning("主力资金流 %s 连续 %d 次无数据（最近: %s），本 session 熔断", clean_code, count, reason[:80])


def _coerce_fund_flow_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, str):
        value = value.replace(",", "").replace("元", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_chinese_amount(value: Any) -> Optional[float]:
    """Parse THS ranking amounts such as '-1533.52万' and '1.04亿'."""
    if value is None:
        return None
    text = str(value).replace(",", "").replace("元", "").replace("+", "").strip()
    if not text or text in {"--", "-"}:
        return None
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier = 1e12
        text = text[:-2]
    elif text.endswith("亿"):
        multiplier = 1e8
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 1e4
        text = text[:-1]
    try:
        return float(text) * multiplier
    except (TypeError, ValueError):
        return None


def _coerce_stock_code(value: Any) -> Optional[str]:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return str(int(float(text))).zfill(6) if text else None
    except (TypeError, ValueError):
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, str):
        value = value.replace(",", "").replace("元", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mark_fund_flow_rank_failure(reason: str):
    """Back off the batch source instead of freezing the whole program."""
    global _main_force_fallback_failures, _main_force_fallback_block_until
    _main_force_fallback_failures += 1
    step_index = min(
        _main_force_fallback_failures - 1,
        len(_MAIN_FORCE_FALLBACK_BACKOFF_STEPS) - 1,
    )
    delay = _MAIN_FORCE_FALLBACK_BACKOFF_STEPS[step_index]
    _main_force_fallback_block_until = time.monotonic() + delay
    logger.warning(
        "资金流批量备源 %ds 后重试（第 %d 次失败）: %s",
        delay, _main_force_fallback_failures, reason[:120],
    )


def _load_fund_flow_rank_snapshot() -> Optional[Dict[str, float]]:
    """Load one market-wide 3-day fund flow snapshot for fallback."""
    global _fund_flow_rank_cache_at
    global _main_force_fallback_failures, _main_force_fallback_block_until
    global _fund_flow_rank_source, _fund_flow_rank_last_error
    now = time.monotonic()
    if _fund_flow_rank_cache and now - _fund_flow_rank_cache_at < _FUND_FLOW_RANK_TTL:
        return _fund_flow_rank_cache
    if now < _main_force_fallback_block_until:
        return None

    try:
        import akshare as ak
        providers = [
            {
                "source": "eastmoney",
                "func": ak.stock_individual_fund_flow_rank,
                "kwargs": {"indicator": "3日"},
                "timeout": 20,
                "code_columns": ("代码", "股票代码"),
                "net_columns": _FUND_FLOW_RANK_NET_COLUMNS,
                "amount_parser": _coerce_fund_flow_number,
            },
            {
                "source": "ths",
                "func": ak.stock_fund_flow_individual,
                "kwargs": {"symbol": "3日排行"},
                "timeout": 35,
                "code_columns": ("股票代码", "代码"),
                "net_columns": ("资金流入净额",),
                "amount_parser": _coerce_chinese_amount,
            },
        ]
        errors: List[str] = []

        for provider in providers:
            fallback_error: Optional[BaseException] = None

            def call_provider(**kwargs):
                nonlocal fallback_error
                try:
                    return provider["func"](**kwargs)
                except Exception as exc:
                    fallback_error = exc
                    raise

            call_provider.__name__ = provider["func"].__name__
            df = call_ak_with_retry(
                call_provider,
                retries=0,
                timeout=provider["timeout"],
                **provider["kwargs"],
            )
            if df is None or getattr(df, "empty", True):
                errors.append(f"{provider['source']}:{str(fallback_error or 'empty response')[:80]}")
                continue

            code_col = next(
                (c for c in provider["code_columns"] if c in df.columns), None
            )
            net_col = next(
                (c for c in provider["net_columns"] if c in df.columns), None
            )
            if not code_col or not net_col:
                errors.append(f"{provider['source']}:missing columns {list(df.columns)[:8]}")
                continue

            parsed: Dict[str, float] = {}
            for _, row in df.iterrows():
                raw_code = _coerce_stock_code(row[code_col])
                value = provider["amount_parser"](row[net_col])
                if raw_code and raw_code.isdigit() and value is not None:
                    parsed[raw_code] = value

            if not parsed:
                errors.append(f"{provider['source']}:no parsable rows")
                continue

            _fund_flow_rank_cache.clear()
            _fund_flow_rank_cache.update(parsed)
            _fund_flow_rank_cache_at = now
            _main_force_fallback_failures = 0
            _main_force_fallback_block_until = 0.0
            _fund_flow_rank_source = provider["source"]
            _fund_flow_rank_last_error = ""
            logger.info(
                "资金流批量备源 %s 已缓存 %d 只股票（TTL %d 分钟）",
                provider["source"], len(parsed), _FUND_FLOW_RANK_TTL // 60,
            )
            return _fund_flow_rank_cache

        _mark_fund_flow_rank_failure("; ".join(errors))
        return None
    except Exception as exc:
        _mark_fund_flow_rank_failure(str(exc))
        return None


def _evaluate_main_force_flows(net_flows: List[float]) -> Dict[str, Any]:
    """按最近 5 日累计方向生成投票；强票只表示节奏，不追加票数。"""
    if len(net_flows) < 3:
        return {"vote": 0, "detail": "主力资金数据不足 3 日", "raw": {"net_flows": net_flows}}

    valid_flows = [float(value) for value in net_flows[-5:] if value is not None]
    if not valid_flows:
        return {"vote": 0, "detail": "主力资金数据无效", "raw": {"net_flows": net_flows}}

    total = sum(valid_flows)
    day_count = len(valid_flows)
    inflow_days = sum(1 for value in valid_flows if value > 0)
    outflow_days = sum(1 for value in valid_flows if value < 0)
    recent_same_direction = (
        all(value > 0 for value in valid_flows[-3:])
        or all(value < 0 for value in valid_flows[-3:])
    )
    direction = 1 if total > 0 else (-1 if total < 0 else 0)
    window_label = f"{day_count}日" if day_count != 5 else "5日"
    detail = (
        f"主力{window_label}累计{total/1e8:+.2f}亿"
        f"({inflow_days}/{day_count}日流入,{outflow_days}/{day_count}日流出)"
    )
    return {
        "vote": direction,
        "detail": detail,
        "raw": {
            "net_flows": valid_flows,
            "total": total,
            "inflow_days": inflow_days,
            "outflow_days": outflow_days,
            "strong": direction != 0 and recent_same_direction,
        },
    }


def _market_for_stock(code: str) -> str:
    first = str(code).zfill(6)[:1]
    if first == "6":
        return "sh"
    if first in ("4", "8", "9"):
        return "bj"
    return "sz"


def _mark_detailed_fund_flow_failure(reason: str):
    """Back off optional AKShare detail calls; never block the main flow source."""
    global _detailed_fund_flow_failures, _detailed_fund_flow_block_until
    _detailed_fund_flow_failures += 1
    step_index = min(
        _detailed_fund_flow_failures - 1,
        len(_DETAILED_FUND_FLOW_BACKOFF_STEPS) - 1,
    )
    delay = _DETAILED_FUND_FLOW_BACKOFF_STEPS[step_index]
    _detailed_fund_flow_block_until = time.monotonic() + delay
    logger.debug("个股资金流明细备用源退避 %ds: %s", delay, reason[:80])


def _fetch_detailed_fund_flow(code: str) -> Optional[Dict[str, Any]]:
    """
    Fetch daily fund-flow details from AKShare/Eastmoney.

    This is an enrichment source only.  Iwencai remains the primary fund-flow
    path because this endpoint may disconnect intermittently.
    """
    clean_code = str(code).zfill(6)
    now = time.monotonic()
    if now < _detailed_fund_flow_block_until:
        return None

    cached = _detailed_fund_flow_cache.get(clean_code)
    if cached and now - cached["ts"] < _DETAILED_FUND_FLOW_TTL:
        return cached["data"]

    try:
        import akshare as ak

        frame = call_ak_with_retry(
            ak.stock_individual_fund_flow,
            stock=clean_code,
            market=_market_for_stock(clean_code),
            retries=0,
            timeout=10,
        )
        if frame is None or frame.empty:
            _mark_detailed_fund_flow_failure("无数据")
            return None

        column_map = {
            "日期": "date",
            "主力净流入-净额": "main_net",
            "超大单净流入-净额": "super_large_net",
            "大单净流入-净额": "large_net",
            "中单净流入-净额": "medium_net",
            "小单净流入-净额": "small_net",
        }
        rows = []
        for _, row in frame.tail(5).iterrows():
            item = {}
            for cn_key, en_key in column_map.items():
                if cn_key in row:
                    item[en_key] = _coerce_fund_flow_number(row[cn_key])
            if item.get("main_net") is not None:
                rows.append(item)
        if not rows:
            _mark_detailed_fund_flow_failure("明细列缺失")
            return None

        data = {
            "source": "stock_individual_fund_flow",
            "rows": rows,
            "main_flows_5d": [row["main_net"] for row in rows],
            "super_large_flows_5d": [
                row.get("super_large_net") for row in rows
                if row.get("super_large_net") is not None
            ],
            "large_flows_5d": [
                row.get("large_net") for row in rows
                if row.get("large_net") is not None
            ],
        }
        _detailed_fund_flow_cache[clean_code] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        _mark_detailed_fund_flow_failure(str(exc))
        return None


def _fetch_main_force_flow_fallback(code: str) -> Optional[Dict[str, Any]]:
    """Fallback to one batched 3-day cumulative fund-flow snapshot."""
    clean_code = str(code).zfill(6)
    snapshot = _load_fund_flow_rank_snapshot()
    if snapshot is None:
        return None
    total = snapshot.get(clean_code)
    if total is None:
        return None

    if total > 0:
        vote = 1
        direction = "净流入"
    elif total < 0:
        vote = -1
        direction = "净流出"
    else:
        vote = 0
        direction = "持平"
    source_label = "东财主力3日" if _fund_flow_rank_source == "eastmoney" else "同花顺3日净额"
    source_api = (
        "stock_individual_fund_flow_rank(3日)"
        if _fund_flow_rank_source == "eastmoney"
        else "stock_fund_flow_individual(3日排行)"
    )
    return {
        "vote": vote,
        "detail": f"{source_label}{direction}（合计 {total/1e8:.2f} 亿，批量源）",
        "raw": {
            "net_flows": [total],
            "total": total,
            "source": source_api,
        },
    }


def _reset_institutional_state():
    """重置所有状态（仅供测试用）"""
    global _api_disabled, _api_fail_count
    global _fund_flow_rank_cache_at
    global _main_force_fallback_failures, _main_force_fallback_block_until
    global _fund_flow_rank_source, _fund_flow_rank_last_error
    _api_disabled = {k: False for k in _api_disabled}
    _api_fail_count = {k: 0 for k in _api_fail_count}
    _main_force_failures.clear()
    _fund_flow_rank_cache.clear()
    _fund_flow_rank_cache_at = 0.0
    _main_force_fallback_failures = 0
    _main_force_fallback_block_until = 0.0
    _fund_flow_rank_source = ""
    _fund_flow_rank_last_error = "尚未加载"
    _institutional_session_cache.clear()
    _detailed_fund_flow_cache.clear()
    _detailed_fund_flow_cache_at = 0.0
    _detailed_fund_flow_failures = 0
    _detailed_fund_flow_block_until = 0.0
    _top10_holder_cache.clear()
    _top10_holder_cache_date = ""


# ---------------------------------------------------------------------------
# 数据源 1: 融资余额（F9 整改 2026-07-22：替换北向资金）
# ---------------------------------------------------------------------------
# 原数据源：ak.stock_hsgt_individual_em（北向个股持股明细）
#   问题：2024-08-16 起交易所停止披露北向实时数据，接口返回拟合值（R²≈0.32）
# 新数据源：ak.stock_margin_detail_sse + ak.stock_margin_detail_szse（融资融券明细）
#   判断：融资余额环比变化（今日 vs 5日前）
#     - 融资余额增加 >2% → 看多(+1)，杠杆资金加仓
#     - 融资余额减少 >2% → 看空(-1)，杠杆资金撤退
#     - 其他 → 中性(0)
#
# 缓存策略：session 级全市场缓存（当日所有股票共享一份）
#   融资余额接口按日期返回全市场数据，单次调用即可获取所有股票
#   避免每只股票都调一次接口（17只×1次=17次）

_margin_market_cache: Dict[str, Dict[str, float]] = {}  # {date: {code: 融资余额}}
_margin_cache_date: str = ""

def _find_recent_trading_day(target_date: str, max_lookback: int = 10, skip_today: bool = False) -> str:
    """找最近的交易日（周末/假期回退）。简单版：跳过周六日。

    Args:
        target_date: 目标日期 YYYYMMDD
        max_lookback: 最多回退天数
        skip_today: 是否跳过target_date本身（今天数据可能未更新）
    """
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(target_date, "%Y%m%d")
    except Exception:
        return target_date
    start_offset = 1 if skip_today else 0
    for i in range(start_offset, max_lookback):
        d = dt - timedelta(days=i)
        if d.weekday() < 5:  # 周一到周五
            return d.strftime("%Y%m%d")
    return target_date

def _fetch_margin_balance(code: str) -> Dict[str, Any]:
    """
    查询个股融资余额变化，判断杠杆资金动向。

    数据源：
      - 沪市：ak.stock_margin_detail_sse(date)
      - 深市：ak.stock_margin_detail_szse(date)

    判断：最近交易日融资余额 vs 5个交易日前融资余额
      - 增加 >2% → 看多(+1)
      - 减少 >2% → 看空(-1)
      - 其他 → 中性(0)

    F9 v2 改进：
      - 自动找最近交易日（跳过周末）
      - 深市接口不稳定，失败时降级为中性
      - session 级全市场缓存
    """
    if _api_disabled["north_bound"]:
        return {"vote": 0, "detail": "融资余额接口已短路", "raw": {}}

    try:
        import akshare as ak
        from datetime import datetime, timedelta

        today = datetime.now().strftime("%Y%m%d")
        # session 缓存：当日只拉一次全市场数据
        global _margin_market_cache, _margin_cache_date
        if _margin_cache_date != today:
            _margin_market_cache = {}
            _margin_cache_date = today

        # 找最近交易日（今天和7天前，各回退找交易日）
        latest_target = today
        prev_target = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        latest_date = _find_recent_trading_day(latest_target, skip_today=True)  # 跳过今天（数据未更新）
        prev_date = _find_recent_trading_day(prev_target)

        # 拉取两个交易日的全市场数据
        for fetch_date in [latest_date, prev_date]:
            if fetch_date in _margin_market_cache:
                continue
            _margin_market_cache[fetch_date] = {}
            # 沪市
            try:
                df_sse = call_ak_with_retry(ak.stock_margin_detail_sse, date=fetch_date)
                if df_sse is not None and not df_sse.empty:
                    code_col = "标的证券代码" if "标的证券代码" in df_sse.columns else df_sse.columns[1]
                    bal_col = "融资余额" if "融资余额" in df_sse.columns else None
                    if bal_col:
                        for _, row in df_sse.iterrows():
                            c = str(row[code_col]).zfill(6)
                            bal = float(row[bal_col])
                            if c and bal > 0:
                                _margin_market_cache[fetch_date][c] = bal
            except Exception:
                pass
            # 深市（不稳定，失败不影响沪市数据）
            try:
                df_szse = call_ak_with_retry(ak.stock_margin_detail_szse, date=fetch_date)
                if df_szse is not None and not df_szse.empty:
                    code_col = "证券代码" if "证券代码" in df_szse.columns else df_szse.columns[1]
                    bal_col = "融资余额" if "融资余额" in df_szse.columns else None
                    if bal_col:
                        for _, row in df_szse.iterrows():
                            c = str(row[code_col]).zfill(6)
                            bal = float(row[bal_col])
                            if c and bal > 0:
                                _margin_market_cache[fetch_date][c] = bal
            except Exception:
                pass  # 深市失败静默降级

        latest_bal = _margin_market_cache.get(latest_date, {}).get(code, 0)
        prev_bal = _margin_market_cache.get(prev_date, {}).get(code, 0)

        if latest_bal <= 0:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": "无融资余额数据（非两融标的或深市接口失败）", "raw": {}}

        if prev_bal <= 0:
            _mark_api_success("north_bound")
            return {"vote": 0, "detail": f"融资余额{latest_bal/1e8:.2f}亿（无对比数据）", "raw": {"latest": latest_bal}}

        change_pct = (latest_bal - prev_bal) / prev_bal
        _mark_api_success("north_bound")

        if change_pct > 0.02:  # 融资余额增加 >2%
            return {
                "vote": 1,
                "detail": f"融资余额增加{change_pct*100:.1f}%（{prev_bal/1e8:.2f}→{latest_bal/1e8:.2f}亿）",
                "raw": {"latest": latest_bal, "prev": prev_bal, "change_pct": change_pct},
            }
        elif change_pct < -0.02:  # 融资余额减少 >2%
            return {
                "vote": -1,
                "detail": f"融资余额减少{change_pct*100:.1f}%（{prev_bal/1e8:.2f}→{latest_bal/1e8:.2f}亿）",
                "raw": {"latest": latest_bal, "prev": prev_bal, "change_pct": change_pct},
            }
        else:
            return {
                "vote": 0,
                "detail": f"融资余额持平（变化{change_pct*100:.1f}%，{latest_bal/1e8:.2f}亿）",
                "raw": {"latest": latest_bal, "prev": prev_bal, "change_pct": change_pct},
            }
    except Exception as e:
        err_msg = str(e)[:80]
        _mark_api_failure("north_bound", err_msg)
        return {"vote": 0, "detail": f"融资余额接口异常: {err_msg}", "raw": {}}


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
        df = call_ak_with_retry(ak.stock_lhb_detail_em, start_date=start_date, end_date=today)
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
# 数据源 3: 主力资金流（最近 5 日累计）
# 数据源已切换至问财 OpenAPI，不再使用东财 push2his HTTP 爬虫
# ---------------------------------------------------------------------------

def _fetch_main_force_flow(code: str) -> Dict[str, Any]:
    """
    查询个股主力资金流，按最近 5 日累计方向投票。

    主源：问财 OpenAPI（Bearer Token 认证）。
    增强：AKShare 逐日资金流提供超大单/大单/中单/小单拆分。

    Returns:
        {"vote": 1/-1/0, "detail": str, "raw": dict}
        vote: 1=最近 5 日累计净流入看多, -1=累计净流出看空, 0=无趋势
    """
    clean_code = str(code).zfill(6)
    if _api_disabled["main_force"]:
        return {"vote": 0, "detail": "主力资金接口已短路（连续多只失败）", "raw": {}}
    if _main_force_failures.get(clean_code, 0) >= _MAIN_FORCE_FAILURE_THRESHOLD:
        return {"vote": 0, "detail": "主力资金接口已短路（该股连续无数据）", "raw": {}}

    try:
        from ..data_layer.iwencai_api import query_stock_fund_flow

        fund = query_stock_fund_flow(clean_code, call_type="normal")
        if not fund:
            fund = query_stock_fund_flow(clean_code, call_type="retry")

        fallback = None
        if not fund:
            fallback = _fetch_main_force_flow_fallback(clean_code)
            if fallback is None:
                fallback_reason = _fund_flow_rank_last_error
                if not fallback_reason:
                    _mark_main_force_failure(clean_code, "批量快照成功但该股无数据")
                else:
                    logger.warning(
                        "主力资金流 %s 主备源不可用（不标记单股熔断）: %s",
                        clean_code, fallback_reason[:120],
                    )
                return {
                    "vote": 0,
                    "detail": f"主力资金接口异常(主备源无数据: {fallback_reason[:80]})",
                    "raw": {"fallback_error": fallback_reason},
                }
            _mark_api_success("main_force")
            return fallback

        net_flows = (
            fund.get("net_flows_5d")
            or fund.get("net_flows_3d")
            or ([fund["main_net"]] if fund.get("main_net") is not None else [])
        )
        super_large_flows = fund.get("super_large_flows_5d") or []
        large_flows = fund.get("large_flows_5d") or []
        if not super_large_flows or not large_flows:
            detail = _fetch_detailed_fund_flow(clean_code)
            if detail:
                super_large_flows = super_large_flows or detail.get("super_large_flows_5d", [])
                large_flows = large_flows or detail.get("large_flows_5d", [])
                net_flows = net_flows or detail.get("main_flows_5d", [])

        result = _evaluate_main_force_flows(net_flows)
        result["raw"].update({
            "net_flows_5d": net_flows,
            "fund_flow_5d": fund.get("fund_flow_points_5d", []),
            "super_large_flows_5d": super_large_flows,
            "large_flows_5d": large_flows,
            "latest_super_large_net": super_large_flows[-1] if super_large_flows else fund.get("super_large_net"),
            "latest_large_net": large_flows[-1] if large_flows else fund.get("large_net"),
        })
        return result
    except Exception as e:
        err_msg = str(e)[:80]
        fallback = _fetch_main_force_flow_fallback(clean_code)
        if fallback is not None:
            _mark_api_success("main_force")
            return fallback
        _mark_main_force_failure(clean_code, err_msg)
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
        df = call_ak_with_retry(ak.stock_zh_a_gdhs_detail_em, symbol=code)
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

        # P1-4 数据异常守卫：新股上市前后户数跳变（如 20→724313）、拆股转增、定增等
        # 会产生异常巨幅跳变，若按正常增减判"筹码分散"会投出误导性 -1 票 → 一律投中性 0。
        # ① 数量级异常：A股上市公司股东户数不可能 <100
        if latest_count < 100 or prev_count < 100:
            return {
                "vote": 0,
                "detail": f"股东户数数量级异常（{prev_count:.0f}→{latest_count:.0f}，疑似上市前基准/缺数，不参与投票）",
                "raw": {"latest": latest_count, "prev": prev_count, "change_pct": change_pct, "anomaly": True},
            }
        # ② 单期跳变上限：单期增幅 >300% 或降幅 >80% 判为数据/结构性变化
        if change_pct > 3.0 or change_pct < -0.8:
            return {
                "vote": 0,
                "detail": f"股东户数跳变异常 {change_pct*100:+.1f}%（{prev_count:.0f}→{latest_count:.0f}，数据/结构性变化，不参与投票）",
                "raw": {"latest": latest_count, "prev": prev_count, "change_pct": change_pct, "anomaly": True},
            }

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


def _latest_report_dates() -> tuple:
    """Return the latest published quarterly date and the one before it."""
    today = datetime.now()
    if today.month <= 4:
        latest = datetime(today.year - 1, 12, 31)
    elif today.month <= 8:
        latest = datetime(today.year, 3, 31)
    elif today.month <= 10:
        latest = datetime(today.year, 6, 30)
    else:
        latest = datetime(today.year, 9, 30)

    if latest.month == 12:
        prev = datetime(latest.year - 1, 9, 30)
    elif latest.month <= 3:
        prev = datetime(latest.year - 1, 12, 31)
    elif latest.month <= 6:
        prev = datetime(latest.year, 3, 31)
    else:
        prev = datetime(latest.year, 6, 30)
    return latest.strftime("%Y%m%d"), prev.strftime("%Y%m%d")


def _symbol_for_holder_api(code: str) -> str:
    clean_code = str(code).zfill(6)
    first = clean_code[:1]
    if first == "6":
        return f"sh{clean_code}"
    if first in ("4", "8", "9"):
        return f"bj{clean_code}"
    return f"sz{clean_code}"


def _fetch_top10_institutional_ratio(code: str) -> Optional[Dict[str, Any]]:
    """
    Fetch top-10 free-float institutional ownership for display.

    Disclosure data is quarterly and should never join the intraday vote.
    """
    clean_code = str(code).zfill(6)
    latest_date, prev_date = _latest_report_dates()
    cache_key = f"{clean_code}:{latest_date}:{prev_date}"
    today = datetime.now().strftime("%Y%m%d")
    cached = _top10_holder_cache.get(cache_key)
    if cached and _top10_holder_cache_date == today:
        return cached

    try:
        import akshare as ak

        symbol = _symbol_for_holder_api(clean_code)
        inst_keywords = (
            "证券投资基金", "基金", "社保", "保险", "QFII", "券商",
            "信托", "私募", "资产管理", "银行", "财务公司", "企业年金",
            "投资公司", "香港中央结算",
        )

        def summarize(date: str):
            frame = call_ak_with_retry(
                ak.stock_gdfx_free_top_10_em,
                symbol=symbol,
                date=date,
                retries=0,
                timeout=10,
            )
            if frame is None or frame.empty:
                return None
            ratio_col = "占总流通股本持股比例"
            nature_col = "股东性质"
            if ratio_col not in frame.columns:
                return None
            total = 0.0
            count = 0
            fund_count = 0
            for _, row in frame.iterrows():
                nature = str(row.get(nature_col, ""))
                if nature and not any(keyword in nature for keyword in inst_keywords):
                    continue
                value = _coerce_fund_flow_number(row.get(ratio_col))
                if value is None:
                    continue
                total += value
                count += 1
                if "证券投资基金" in nature or "基金" in nature:
                    fund_count += 1
            return {"date": date, "ratio": round(total, 2), "rows": count, "fund_rows": fund_count}

        latest = summarize(latest_date)
        prev = summarize(prev_date)
        if latest is None and prev is None:
            return None
        result = {
            "latest": latest,
            "previous": prev,
            "change_points": (
                round(latest["ratio"] - prev["ratio"], 2)
                if latest and prev else None
            ),
        }
        _top10_holder_cache[cache_key] = result
        _top10_holder_cache_date = today
        return result
    except Exception as exc:
        logger.debug("前十大机构持仓查询失败 %s: %s", clean_code, str(exc)[:80])
        return None


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
        "north_bound": _fetch_margin_balance(code),
        "lhb": _fetch_lhb_institutional(code),
        "main_force": _fetch_main_force_flow(code),
        "shareholder": _fetch_shareholder_count(code),
    }
    top10_ratio = _fetch_top10_institutional_ratio(code)

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
        "top10_institutional_ratio": top10_ratio,
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
