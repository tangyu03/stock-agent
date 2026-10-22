"""
F2: 机构被套套利规则
====================
触发条件（三重确认）：
  1. 首日冲高回落：当日最高价>开盘价×1.05 且 收盘价<最高价×0.97（冲高5%后回落3%）
  2. 龙虎榜机构大买：机构买入净额>0 且 买方机构数>=3
  3. 机构被套：收盘价 < 机构买入均价（用当日VWAP近似）

套利逻辑：
  机构首日大买被套 → 次日有自救动力 → 套利空间
  目标：次日冲高卖出（+3~5%）

数据源：
  - ak.stock_lhb_jgmmtj_em — 机构买卖统计
  - ak.stock_zh_a_daily — 个股K线（计算VWAP）

注意：
  - 机构买入均价只能用当日VWAP=(开盘+最高+最低+收盘)/4 近似，不精确
  - 回测中用K线数据，实盘用实时行情
"""
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def _calc_vwap(open_price: float, high: float, low: float, close: float) -> float:
    """计算当日VWAP近似值（无成交量加权时用OHLC均值）"""
    return (open_price + high + low + close) / 4


def check_institutional_trapped(code: str, kline_data: list) -> Optional[Dict[str, Any]]:
    """
    检查个股是否满足"机构被套套利"条件

    Args:
        code: 股票代码
        kline_data: K线列表 [{date, open, high, low, close, volume}, ...]

    Returns:
        None 或 {
            "triggered": True,
            "entry_type": "机构被套",
            "detail": str,
            "vwap": float,
            "inst_net_buy": float,
            "buyer_inst": int,
            "entry_price": float,  # 次日开盘价（套利进场）
            "target_price": float,  # +3%目标
            "stop_loss": float,     # -2%止损
        }
    """
    if not kline_data or len(kline_data) < 1:
        return None

    today = kline_data[-1]
    today_open = float(today.get('open', 0) or today.get('开盘', 0))
    today_high = float(today.get('high', 0) or today.get('最高', 0))
    today_low = float(today.get('low', 0) or today.get('最低', 0))
    today_close = float(today.get('close', 0) or today.get('收盘', 0))

    if today_open <= 0 or today_close <= 0:
        return None

    # 条件1：首日冲高回落
    # 冲高>5% 且 回落>3%（收盘<最高×0.97）
    surge_pct = (today_high - today_open) / today_open
    pullback_pct = (today_high - today_close) / today_high if today_high > 0 else 0
    if surge_pct < 0.05 or pullback_pct < 0.03:
        return None  # 不满足冲高回落

    # 条件2：龙虎榜机构大买
    from .lhb_scorer import score_lhb
    lhb = score_lhb(code)
    if lhb.get("stale", True):
        return None  # 无龙虎榜数据

    raw = lhb.get("raw", {})
    inst_net_buy = raw.get("inst_net_buy", 0)
    buyer_inst = raw.get("buyer_inst", 0)
    if inst_net_buy <= 0 or buyer_inst < 3:
        return None  # 机构未大买

    # 条件3：机构被套（收盘 < VWAP近似机构买入均价）
    vwap = _calc_vwap(today_open, today_high, today_low, today_close)
    if today_close >= vwap:
        return None  # 收盘在VWAP上方，机构未被套

    # 触发机构被套套利
    trapped_pct = (vwap - today_close) / vwap * 100
    detail = (f"机构被套: 冲高{surge_pct*100:.1f}%回落{pullback_pct*100:.1f}%, "
              f"机构净买{inst_net_buy/1e8:.2f}亿(买方{buyer_inst}家), "
              f"VWAP{vwap:.2f}>收盘{today_close:.2f}(被套{trapped_pct:.1f}%)")

    return {
        "triggered": True,
        "entry_type": "机构被套",
        "detail": detail,
        "vwap": round(vwap, 2),
        "inst_net_buy": inst_net_buy,
        "buyer_inst": buyer_inst,
        "entry_price": today_close,  # 当日收盘进场（次日开盘实际成交）
        "target_price": round(today_close * 1.03, 2),  # +3%目标
        "stop_loss": round(today_close * 0.98, 2),  # -2%止损
        "lhb_score": lhb.get("score", 50),
    }


def scan_institutional_trapped(stock_codes: list, kline_data_map: dict) -> list:
    """
    批量扫描机构被套套利机会

    Args:
        stock_codes: 股票代码列表
        kline_data_map: {code: kline_data, ...}

    Returns:
        [check_institutional_trapped 结果, ...]
    """
    results = []
    for code in stock_codes:
        kline = kline_data_map.get(code, [])
        result = check_institutional_trapped(code, kline)
        if result and result["triggered"]:
            results.append({"code": code, **result})
    return results
