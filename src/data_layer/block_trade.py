"""大宗交易数据源（P0-6）：近N日折价大宗统计 + 派发确认候选触发。

接入为第五资金源（笔数/折溢价/连续性/营业部）；无龙虎榜无两融的标的
（北交所）优先补位。规则：近20日折价大宗≥5笔 或 累计折价金额>流通市值1%
→ 强制挂派发确认候选。
"""
import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 折价阈值（折溢率 ≤ -3 视为折价大宗，单位百分比）
DISCOUNT_RATIO_THRESHOLD = -3.0
# 近20日折价大宗最低笔数
MIN_DISCOUNT_TRADES = 5
# 累计折价金额 / 流通市值 阈值
FLOAT_CAP_RATIO = 0.01
# 卖方营业部持续出货的最低出现次数（同一营业部）
SELLER_CONSISTENCY_MIN = 2

_DEFAULT_DAYS = 20


def _code_col(df) -> Optional[str]:
    for c in ("证券代码", "代码", "SECURITY_CODE"):
        if c in df.columns:
            return c
    return None


def _parse_block_trade_frame(df, code: str, days: int) -> Optional[Dict[str, Any]]:
    """从市场级大宗明细 DataFrame 过滤出单股近N日统计。

    兼容 akshare stock_dzjy_mrmx(symbol='A股') 返回的列：
    交易日期/证券代码/成交价/折溢率/成交量/成交额/买方营业部/卖方营业部。
    """
    if df is None or df.empty:
        return None
    ccol = _code_col(df)
    if not ccol:
        return None
    code = str(code).zfill(6)
    sub = df[df[ccol].astype(str).str.zfill(6) == code]
    if sub.empty:
        return None
    try:
        ratio = sub["折溢率"].astype(float)
        amount = sub["成交额"].astype(float)
    except (KeyError, TypeError, ValueError):
        ratio = None
        amount = None
    discount_mask = None
    if ratio is not None:
        discount_mask = ratio <= DISCOUNT_RATIO_THRESHOLD
    discount_trades = int(discount_mask.sum()) if discount_mask is not None else 0
    discount_total = float(amount[discount_mask].sum()) if (
        amount is not None and discount_mask is not None
    ) else 0.0
    total_amount = float(amount.sum()) if amount is not None else 0.0
    seller_col = "卖方营业部" if "卖方营业部" in sub.columns else None
    sellers: List[str] = []
    if seller_col is not None:
        sellers = [str(v) for v in sub[seller_col].dropna().tolist()]
    seller_consistency = {}
    for s in sellers:
        seller_consistency[s] = seller_consistency.get(s, 0) + 1
    date_col = "交易日期" if "交易日期" in sub.columns else None
    as_of = ""
    if date_col is not None:
        dates = [str(v)[:10] for v in sub[date_col].dropna().tolist()]
        as_of = max(dates) if dates else ""
    return {
        "trades": int(len(sub)),
        "discount_trades": discount_trades,
        "total_amount": total_amount,
        "discount_total": discount_total,
        "as_of": as_of,
        "sellers": sellers,
        "seller_consistency": seller_consistency,
        "days": days,
        "source": "stock_dzjy_mrmx(市场级每日明细)",
    }


def fetch_block_trade_stats(
    code: str,
    days: int = _DEFAULT_DAYS,
    fetcher: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    """抓取近N日大宗交易并统计。

    fetcher 可注入（测试/回放），默认用 akshare 市场级每日明细
    ak.stock_dzjy_mrmx(symbol='A股', start_date, end_date)。
    """
    if fetcher is None:
        def _default_fetcher(start_date: str, end_date: str):
            import akshare as ak
            return ak.stock_dzjy_mrmx(
                symbol="A股", start_date=start_date, end_date=end_date,
            )
        fetcher = _default_fetcher
    end = date.today()
    start = end - timedelta(days=int(days) + 7)  # 缓冲周末/节假日
    try:
        df = fetcher(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    except Exception as e:
        logger.debug("大宗交易抓取失败 %s: %s", code, str(e)[:80])
        return None
    return _parse_block_trade_frame(df, code, int(days))


def block_trade_distribution_trigger(
    stats: Optional[Dict[str, Any]],
    float_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """近20日折价大宗触发派发确认候选。

    Returns:
        {"triggered": bool, "reason": str, "discount_trades": int,
         "discount_total": float}
    """
    out = {
        "triggered": False,
        "reason": "",
        "discount_trades": 0,
        "discount_total": 0.0,
    }
    if not stats:
        return out
    try:
        discount_trades = int(stats.get("discount_trades") or 0)
        discount_total = float(stats.get("discount_total") or 0)
    except (TypeError, ValueError):
        return out
    out["discount_trades"] = discount_trades
    out["discount_total"] = discount_total
    reasons = []
    if discount_trades >= MIN_DISCOUNT_TRADES:
        reasons.append(f"近{stats.get('days', _DEFAULT_DAYS)}日折价大宗"
                       f"{discount_trades}笔≥{MIN_DISCOUNT_TRADES}笔")
    if float_cap and discount_total > float_cap * FLOAT_CAP_RATIO:
        reasons.append(
            f"折价累计{discount_total / 1e8:.2f}亿>流通市值1%"
        )
    if reasons:
        out["triggered"] = True
        out["reason"] = "；".join(reasons)
    return out


def render_block_trade_line(
    stats: Optional[Dict[str, Any]],
    trigger: Optional[Dict[str, Any]] = None,
) -> str:
    """渲染资金卡大宗栏：近20日折价大宗N笔/合计X亿(营业部持续)。"""
    if not stats:
        return ""
    try:
        trades = int(stats.get("trades") or 0)
        discount_trades = int(stats.get("discount_trades") or 0)
        total = float(stats.get("total_amount") or 0)
    except (TypeError, ValueError):
        return ""
    bits = [f"近{stats.get('days', _DEFAULT_DAYS)}日大宗{trades}笔"]
    if discount_trades:
        bits.append(f"折价{discount_trades}笔")
    if total >= 1e8:
        bits.append(f"合计{total / 1e8:.2f}亿")
    elif total >= 1e4:
        bits.append(f"合计{total / 1e4:.0f}万")
    consistency = stats.get("seller_consistency") or {}
    persistent = [
        seller for seller, cnt in consistency.items()
        if cnt >= SELLER_CONSISTENCY_MIN
    ]
    if persistent:
        bits.append(f"营业部持续({len(persistent)}家)")
    line = "大宗:" + "/".join(bits)
    if trigger and trigger.get("triggered"):
        line += f" ⚠派发确认候选({trigger.get('reason', '')})"
    return line


__all__ = [
    "DISCOUNT_RATIO_THRESHOLD", "MIN_DISCOUNT_TRADES", "FLOAT_CAP_RATIO",
    "fetch_block_trade_stats", "block_trade_distribution_trigger",
    "render_block_trade_line",
]
