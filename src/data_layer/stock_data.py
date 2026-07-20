"""
个股数据补充层

用 AKShare 替代问财的部分行情查询，减少问财配额消耗。
同时提供本地技术指标计算（EMA/ADX/RSI/布林带），替代问财 tech_signals。

设计原则：
- 行情数据（最新价/涨跌幅/成交量/成交额/ST标记）：AKShare 新浪源
- 技术指标（EMA/ADX/RSI/布林带）：本地 K 线自算
- K 线形态（15 种）：本地 K 线自算
- 事件/财报体检/资金流：保留问财（独有能力）
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 缓存全市场实时行情（当日有效）
_spot_cache: Dict[str, Dict] = {}


def batch_get_realtime_quotes(codes: List[str]) -> Dict[str, Dict]:
    """
    按需拉取指定股票的实时行情（腾讯主源 → 东财备选）

    只拉持仓/自选的票，不拉全市场。规避东财全市场 spot_em 大数据量接口被封的问题
    （腾讯 qt.gtimg.cn 最稳定，几乎不封；东财 ulist 作为备选）。

    缓存策略：已缓存的股票不重复拉取，只拉缺失的。
    缓存有效期到当日收盘（避免盘中缓存过期数据）。

    Args:
        codes: 股票代码列表，如 ["688256", "000001"]

    Returns:
        {code: {"current_price": float, "change_pct": float, "volume": float,
                "amount": float, "name": str, "today_high": float, "today_low": float,
                "today_open": float, "prev_close": float, "is_st": bool, "is_suspended": bool}}
    """
    if not codes:
        return {}

    # 只拉取缓存中不存在的股票（避免每次清空导致重复调用）
    to_fetch = [c for c in codes if c not in _spot_cache]
    if to_fetch:
        # 主源：腾讯（最稳定，不封）；备选：东财 ulist
        n = _fetch_qq_quotes(to_fetch)
        still_missing = [c for c in to_fetch if c not in _spot_cache]
        if still_missing:
            _fetch_em_ulist(still_missing)

    return {code: _spot_cache.get(code) for code in codes if code in _spot_cache}


def _qq_code(code: str) -> str:
    """股票代码 → 腾讯格式（sh/sz 前缀）"""
    if code.startswith("6") or code.startswith("5") or code.startswith("11"):
        return "sh" + code
    return "sz" + code


def _fetch_qq_quotes(codes: List[str]) -> int:
    """
    腾讯实时行情 qt.gtimg.cn（主源，最稳定），分批 ≤60 只，写入 _spot_cache

    返回值格式：v_sh600000="1~名称~代码~现价~昨收~今开~成交量~...~涨跌幅~最高~最低~...~成交额~..."
    字段索引：[1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]成交量(手)
              [32]涨跌幅% [33]最高 [34]最低 [37]成交额(万)
    """
    global _spot_cache

    import requests
    import random

    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def _f(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    hit = 0
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        syms = ",".join(_qq_code(c) for c in chunk)
        url = f"http://qt.gtimg.cn/q={syms}"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": random.choice(_UA)})
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                if "=" not in line:
                    continue
                payload = line.split("=", 1)[1].strip().strip('"')
                parts = payload.split("~")
                if len(parts) < 38:
                    continue
                code = parts[2]
                if not code:
                    continue
                name = parts[1]
                volume_hand = _f(parts[6])  # 成交量单位：手
                _spot_cache[code] = {
                    "current_price": _f(parts[3]),
                    "change_pct": _f(parts[32]),
                    "volume": volume_hand * 100,          # 手 → 股
                    "amount": _f(parts[37]) * 10000,      # 万 → 元
                    "today_high": _f(parts[33]),
                    "today_low": _f(parts[34]),
                    "today_open": _f(parts[5]),
                    "prev_close": _f(parts[4]),
                    "name": name,
                    "is_st": "ST" in name or "*ST" in name,
                    "is_suspended": volume_hand == 0,
                }
                hit += 1
        except Exception as e:
            logger.warning("腾讯行情拉取失败（chunk %d-%d）: %s",
                           i + 1, min(i + 60, len(codes)), str(e)[:120])

    if hit:
        logger.info("腾讯实时行情: 请求 %d 只, 命中 %d 只", len(codes), hit)
    return hit


def _em_secid(code: str) -> str:
    """股票代码 → 东财 secid（1.=沪 0.=深/北）"""
    if code.startswith("6") or code.startswith("5") or code.startswith("11"):
        return f"1.{code}"  # 沪市股票/基金/可转债
    return f"0.{code}"       # 深市/创业板/北交所


def _fetch_em_ulist(codes: List[str]) -> None:
    """
    东财 ulist.np 小批量接口，分批拉取（每批 ≤ 50 只），写入 _spot_cache
    """
    global _spot_cache

    import requests
    import json
    import random

    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"

    hit = 0
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        secids = ",".join(_em_secid(c) for c in chunk)
        params = {
            "fltt": "2", "invt": "2",
            "fields": "f12,f14,f2,f3,f5,f6,f15,f16,f17,f18",
            "secids": secids,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        try:
            r = requests.get(url, params=params, timeout=10,
                             headers={"User-Agent": random.choice(_UA)})
            d = json.loads(r.text)
            diff = (d.get("data") or {}).get("diff", []) or []
            for row in diff:
                code = str(row.get("f12", ""))
                if not code:
                    continue
                name = str(row.get("f14", ""))
                # 东财返回 "-" 表示停牌/无数据
                def _f(v):
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return 0.0
                volume = _f(row.get("f5", 0))
                _spot_cache[code] = {
                    "current_price": _f(row.get("f2", 0)),
                    "change_pct": _f(row.get("f3", 0)),
                    "volume": volume,
                    "amount": _f(row.get("f6", 0)),
                    "today_high": _f(row.get("f15", 0)),
                    "today_low": _f(row.get("f16", 0)),
                    "today_open": _f(row.get("f17", 0)),
                    "prev_close": _f(row.get("f18", 0)),
                    "name": name,
                    "is_st": "ST" in name or "*ST" in name,
                    "is_suspended": volume == 0,
                }
                hit += 1
        except Exception as e:
            logger.warning("东财 ulist 拉取失败（chunk %d-%d）: %s",
                           i + 1, min(i + 50, len(codes)), str(e)[:120])

    logger.info("东财实时行情: 请求 %d 只, 命中 %d 只", len(codes), hit)


# ============================================================
# 本地技术指标计算（替代问财 tech_signals）
# 需求 §12.2 #6：EMA/ADX/RSI/布林带三维投票
# ============================================================

def calc_ema(values: List[float], period: int) -> List[float]:
    """计算 EMA"""
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for i in range(1, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes: List[float], period: int = 14) -> float:
    """计算 RSI（0-100）"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """
    计算 ADX（趋势强度指标）
    ADX > 25 表示趋势强劲
    """
    if len(closes) < period * 2 + 1:
        return 20.0  # 数据不足返回中性

    # True Range
    trs = []
    plus_dms, minus_dms = [], []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

        plus_dm = highs[i] - highs[i - 1]
        minus_dm = lows[i - 1] - lows[i]
        if plus_dm > minus_dm and plus_dm > 0:
            plus_dms.append(plus_dm)
        else:
            plus_dms.append(0)
        if minus_dm > plus_dm and minus_dm > 0:
            minus_dms.append(minus_dm)
        else:
            minus_dms.append(0)

    # 简化：取最近 period 天平均
    if len(trs) < period:
        return 20.0

    atr = sum(trs[-period:]) / period
    if atr == 0:
        return 20.0

    plus_di = (sum(plus_dms[-period:]) / period) / atr * 100
    minus_di = (sum(minus_dms[-period:]) / period) / atr * 100

    if plus_di + minus_di == 0:
        return 20.0

    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
    return dx


def calc_bollinger(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Dict:
    """
    计算布林带
    返回 {"upper": float, "middle": float, "lower": float, "position": "above"/"in"/"below"}
    """
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "position": "in"}

    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = variance ** 0.5
    upper = middle + std_dev * std
    lower = middle - std_dev * std

    current = closes[-1]
    if current > upper:
        position = "above"
    elif current < lower:
        position = "below"
    else:
        position = "in"

    # 带宽（借鉴 a-stock-kline-analyzer：>15%高波动，<10%低波动）
    bandwidth = (upper - lower) / middle * 100 if middle > 0 else 0
    # 当前位置百分比（0%=下轨，100%=上轨）
    position_pct = (current - lower) / (upper - lower) * 100 if upper != lower else 50

    return {
        "upper": upper, "middle": middle, "lower": lower,
        "position": position,
        "bandwidth": round(bandwidth, 1),
        "position_pct": round(position_pct, 0),
    }


def calc_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """
    计算 MACD 指标（借鉴 a-stock-kline-analyzer 的 6 级信号粒度）

    Returns:
        {"dif": float, "dea": float, "macd": float, "signal": str,
         "hist_direction": "扩大"|"缩小", "state": "金叉"|"金叉延续"|"死叉"|"死叉延续"}
    """
    if len(closes) < slow + signal:
        return {}
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = ema_fast[-1] - ema_slow[-1]
    dif_list = [ema_fast[i] - ema_slow[i] for i in range(len(ema_fast))]
    dea_list = calc_ema(dif_list, signal)
    dea = dea_list[-1]
    macd_bar = 2 * (dif - dea)
    prev_bar = 2 * (dif_list[-2] - dea_list[-2]) if len(dif_list) >= 2 else macd_bar

    # 柱状图动能方向（借鉴 a-stock-kline-analyzer）
    if macd_bar > 0 and macd_bar > prev_bar:
        hist_direction = "红柱扩大"
    elif macd_bar > 0 and macd_bar <= prev_bar:
        hist_direction = "红柱缩小"
    elif macd_bar < 0 and macd_bar < prev_bar:
        hist_direction = "绿柱扩大"
    elif macd_bar < 0 and macd_bar >= prev_bar:
        hist_direction = "绿柱缩小"
    else:
        hist_direction = "持平"

    # 信号判断（6 级：金叉/金叉延续/死叉/死叉延续 + 柱动能）
    if len(dea_list) >= 2:
        prev = dif_list[-2] - dea_list[-2]
        curr = dif - dea
        if prev <= 0 and curr > 0:
            sig = "金叉"
            state = "金叉"
        elif prev >= 0 and curr < 0:
            sig = "死叉"
            state = "死叉"
        elif curr > 0:
            sig = "多头"
            state = "金叉延续"
        else:
            sig = "空头"
            state = "死叉延续"
    else:
        sig = "多头" if macd_bar > 0 else "空头"
        state = sig
    return {
        "dif": round(dif, 3),
        "dea": round(dea, 3),
        "macd": round(macd_bar, 3),
        "signal": sig,
        "state": state,
        "hist_direction": hist_direction,
    }


def detect_chan_divergence(closes: List[float], highs: List[float], lows: List[float],
                           swing_window: int = 5) -> Dict:
    """
    缠论背驰检测（纯本地计算）

    核心逻辑：比较相邻同向走势段的 MACD 柱面积。
    - 价格创新高但 MACD 面积缩小 → 顶背驰（上涨衰竭）
    - 价格创新低但 MACD 面积缩小 → 底背驰（下跌衰竭）

    参考 chan-theory skill: 同时满足趋势力度+空间+时间中 2 项以上 → 背驰确认

    Args:
        closes: 收盘价序列
        highs:  最高价序列
        lows:   最低价序列
        swing_window: 摆动识别窗口（默认5天识别局部极值）

    Returns:
        {"type": "顶背驰"|"底背驰"|"无",
         "confidence": "高"|"中"|"低",
         "detail": str}
    """
    n = len(closes)
    if n < 30:
        return {"type": "无", "confidence": "低", "detail": "数据不足(需>=30根K线)"}

    # 1. 计算 MACD 序列
    ema_fast = calc_ema(closes, 12)
    ema_slow = calc_ema(closes, 26)
    dif_list = [ema_fast[i] - ema_slow[i] for i in range(len(ema_fast))]
    dea_list = calc_ema(dif_list, 9)
    # MACD 柱 = (DIF - DEA) × 2
    hist_list = [2 * (dif_list[i] - dea_list[i]) for i in range(min(len(dif_list), len(dea_list)))]
    # 对齐到 closes
    offset = len(closes) - len(hist_list)
    hist_list = [0] * max(0, offset) + hist_list

    # 2. 识别摆动点（方向转折检测 + 首尾 implied 摆动点）
    swings = []  # [(idx, price, "high"|"low"), ...]
    last_direction = None
    first_dir = None

    for i in range(swing_window, n - swing_window):
        before_avg = sum(closes[i - swing_window:i]) / swing_window
        after_avg = sum(closes[i + 1:i + 1 + swing_window]) / swing_window if i + 1 + swing_window <= n else closes[i]
        current_dir = "up" if after_avg > before_avg else "down"
        if first_dir is None:
            first_dir = current_dir

        if last_direction and current_dir != last_direction:
            if last_direction == "up":
                local_idx = max(range(max(0, i - swing_window), min(n, i + 1)),
                               key=lambda j: highs[j])
                swings.append((local_idx, highs[local_idx], "high"))
            else:
                local_idx = min(range(max(0, i - swing_window), min(n, i + 1)),
                               key=lambda j: lows[j])
                swings.append((local_idx, lows[local_idx], "low"))
        last_direction = current_dir

    if not swings:
        return {"type": "无", "confidence": "低", "detail": "未检测到方向转折"}

    # 在数据首尾补 implied 摆动点（没有转折的起始段/末尾段也需要表示）
    start_dir = first_dir or "up"
    if start_dir == "up":
        start_idx = min(range(swing_window), key=lambda j: lows[j])
        swings.insert(0, (start_idx, lows[start_idx], "low"))
    else:
        start_idx = max(range(swing_window), key=lambda j: highs[j])
        swings.insert(0, (start_idx, highs[start_idx], "high"))

    end_dir = last_direction or "up"
    if end_dir == "up":
        end_idx = max(range(n - swing_window, n), key=lambda j: highs[j])
        swings.append((end_idx, highs[end_idx], "high"))
    else:
        end_idx = min(range(n - swing_window, n), key=lambda j: lows[j])
        swings.append((end_idx, lows[end_idx], "low"))

    if len(swings) < 4:
        return {"type": "无", "confidence": "低", "detail": "摆动点不足"}

    # 3. 取最近的同向走势段进行比较
    # 走势段：从一个摆点到下一个反向摆点
    last = swings[-1]
    second_last = swings[-2]
    third_last = swings[-3] if len(swings) >= 3 else None
    fourth_last = swings[-4] if len(swings) >= 4 else None

    # 计算一段走势的 MACD 面积（绝对值之和）和价格变化
    def segment_info(start_swing, end_swing):
        si, sp, st = start_swing
        ei, ep, et = end_swing
        if si >= ei:
            return None
        area = sum(abs(hist_list[j]) for j in range(si, ei + 1))
        price_chg = abs(ep - sp) / sp if sp > 0 else 0
        duration = ei - si
        return {"area": area, "price_chg": price_chg, "duration": duration,
                "start_idx": si, "end_idx": ei, "direction": st}

    # 找最近的同方向走势段对
    # 当前段: last 是 low → 上涨段(从倒数第二个low到last high)
    #          last 是 high → 下跌段(从倒数第二个high到last low)
    current_seg = None
    prev_seg = None

    if last[2] == "high" and fourth_last and third_last and second_last:
        # last=高点, second_last=低点 → 当前上涨段: second_last→last
        current_seg = segment_info(second_last, last)
        # 前一同向段: fourth_last→third_last (都是上涨)
        if fourth_last[2] == "low" and third_last[2] == "high":
            prev_seg = segment_info(fourth_last, third_last)

    elif last[2] == "low" and fourth_last and third_last and second_last:
        # last=低点, second_last=高点 → 当前下跌段: second_last→last
        current_seg = segment_info(second_last, last)
        # 前一同向段: fourth_last→third_last (都是下跌)
        if fourth_last[2] == "high" and third_last[2] == "low":
            prev_seg = segment_info(fourth_last, third_last)

    if not current_seg or not prev_seg:
        return {"type": "无", "confidence": "低", "detail": "未找到可比走势段"}

    # 4. 背驰判断：价格创新高/低 + MACD面积缩小
    price_confirm = current_seg["price_chg"] > prev_seg["price_chg"] * 0.8  # 价格力度相当或更强
    area_shrink = current_seg["area"] < prev_seg["area"] * 0.8              # 面积缩小20%以上
    time_confirm = current_seg["duration"] <= prev_seg["duration"] * 1.5    # 时间不显著延长

    # 同时满足2项以上 → 背驰
    conditions_met = sum([area_shrink, price_confirm, time_confirm])

    if conditions_met < 2:
        return {"type": "无", "confidence": "低", "detail": "未满足背驰条件"}

    if current_seg["direction"] == "low":
        # 当前是上涨段（从low到high），但MACD面积缩小 → 顶背驰
        type_ = "顶背驰"
        signal = "看跌"
        area_ratio = current_seg["area"] / prev_seg["area"] if prev_seg["area"] > 0 else 1
        detail = (f"上涨段MACD面积{current_seg['area']:.1f} < 前段{prev_seg['area']:.1f}"
                  f"(比值{area_ratio:.2f})，上涨力度衰竭")
    else:
        # 当前是下跌段（从high到low），但MACD面积缩小 → 底背驰
        type_ = "底背驰"
        signal = "看涨"
        area_ratio = current_seg["area"] / prev_seg["area"] if prev_seg["area"] > 0 else 1
        detail = (f"下跌段MACD面积{current_seg['area']:.1f} < 前段{prev_seg['area']:.1f}"
                  f"(比值{area_ratio:.2f})，下跌力度衰竭")

    conf = "高" if conditions_met >= 3 else "中"

    return {"type": type_, "signal": signal, "confidence": conf,
            "detail": detail, "area_ratio": round(area_ratio, 2)}


def calc_kdj(highs: List[float], lows: List[float], closes: List[float],
             period: int = 9, k_smooth: int = 3, d_smooth: int = 3) -> Dict:
    """
    计算 KDJ 指标（纯本地计算，stochastic 公式）

    RSV = (C - L_n) / (H_n - L_n) * 100
    K = EMA(RSV, k_smooth)  用平滑系数
    D = EMA(K, d_smooth)
    J = 3*K - 2*D

    Returns:
        {"k": float, "d": float, "j": float, "signal": "超买"|"超卖"|"金叉"|"死叉"|"中性"}
    """
    n = len(closes)
    if n < period + 2:
        return {}
    # 计算 RSV 序列
    rsv_list = []
    for i in range(period - 1, n):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        rsv_list.append(rsv)
    # 递推 K/D 值（用 EMA 平滑）
    k_list = [50.0]
    for rsv in rsv_list:
        k_list.append(k_list[-1] * (1 - 1/k_smooth) + rsv * (1/k_smooth))
    k_list = k_list[1:]  # 去掉初始值
    d_list = [50.0]
    for k in k_list:
        d_list.append(d_list[-1] * (1 - 1/d_smooth) + k * (1/d_smooth))
    d_list = d_list[1:]
    j_list = [3 * k_list[i] - 2 * d_list[i] for i in range(len(k_list))]
    k_val = k_list[-1]
    d_val = d_list[-1]
    j_val = j_list[-1]
    # 信号判断
    if k_val > 80 and d_val > 80:
        sig = "超买"
    elif k_val < 20 and d_val < 20:
        sig = "超卖"
    elif len(k_list) >= 2 and k_list[-2] <= d_list[-2] and k_val > d_val:
        sig = "金叉"
    elif len(k_list) >= 2 and k_list[-2] >= d_list[-2] and k_val < d_val:
        sig = "死叉"
    else:
        sig = "中性"
    return {
        "k": round(k_val, 2),
        "d": round(d_val, 2),
        "j": round(j_val, 2),
        "signal": sig,
    }


def _rsi_zone(rsi: float, overbought: float = 70, oversold: float = 30) -> str:
    """
    RSI 6 区细分。

    Args:
        overbought: 超买线（默认 70，来自 config voting.rsi.overbought）
        oversold: 超卖线（默认 30，来自 config voting.rsi.oversold）
    """
    extreme_ob = overbought + 10  # 极度超买 = 超买线 + 10
    extreme_os = oversold - 10    # 极度超卖 = 超卖线 - 10
    mid = (overbought + oversold) / 2

    if rsi > extreme_ob:
        return "极度超买"
    elif rsi > overbought:
        return "超买"
    elif rsi > mid:
        return "强势"
    elif rsi > oversold:
        return "弱势"
    elif rsi > extreme_os:
        return "超卖"
    return "极度超卖"


def calc_volatility(closes: List[float], period: int = 20) -> float:
    """
    计算年化波动率（借鉴 a-stock-kline-analyzer）
    σ_annual = std(daily_returns) × √252 × 100%
    """
    if len(closes) < period + 1:
        return 0.0
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-period, 0)]
    mean_ret = sum(returns) / period
    variance = sum((r - mean_ret) ** 2 for r in returns) / (period - 1) if period > 1 else 0
    return (variance ** 0.5) * (252 ** 0.5) * 100


def calc_rs_line(stock_kline: List[Dict], index_kline: List[Dict], period: int = 10) -> Dict:
    """
    计算相对强度线（RS line，IBD 核心买入前检查）

    RS = (个股收盘价 / 指数收盘价)，标准化后与自身均线比较。
    如果 RS line 处于上升趋势（站上其 MA），说明个股相对大盘走强——这是 IBD 买入的前提条件。

    Args:
        stock_kline: 个股K线 [{close, date}, ...]
        index_kline: 指数K线 [{close, date}, ...]
        period: RS均线周期，默认10日

    Returns:
        {
            "rs_uptrend": bool,           # RS线是否处于上升趋势（> MA）
            "rs_ma_trend": str,           # "上升"/"下降"/"走平"
            "rs_latest": float,           # 最新RS值
            "rs_ma": float,               # RS均线值
        }
    """
    # 按日期对齐两边的收盘价
    stock_closes = {}
    for k in stock_kline:
        d = str(k.get("date", ""))[:10]
        c = float(k.get("close", k.get("收盘", 0)))
        if d and c > 0:
            stock_closes[d] = c

    index_closes = {}
    for k in index_kline:
        d = str(k.get("date", ""))[:10]
        c = float(k.get("close", k.get("收盘", 0)))
        if d and c > 0:
            index_closes[d] = c

    # 取交集日期，计算 RS 序列
    common_dates = sorted(set(stock_closes) & set(index_closes))
    if len(common_dates) < period + 5:
        return {"rs_uptrend": False, "rs_ma_trend": "数据不足", "rs_latest": 0, "rs_ma": 0}

    rs_values = []
    for d in common_dates:
        rs_values.append(stock_closes[d] / index_closes[d])

    if len(rs_values) < period + 1:
        return {"rs_uptrend": False, "rs_ma_trend": "数据不足", "rs_latest": 0, "rs_ma": 0}

    # 计算 RS 均线
    rs_ma = sum(rs_values[-period:]) / period
    rs_latest = rs_values[-1]

    # 趋势判断：RS 是否站上均线 + 近期斜率
    if rs_latest > rs_ma * 1.005:
        # 检查均线斜率：前5日均线 vs 后5日均线
        if period >= 10:
            ma_early = sum(rs_values[-period:-period//2]) / (period//2)
            ma_late = sum(rs_values[-period//2:]) / (period//2)
            if ma_late > ma_early * 1.002:
                ma_trend = "上升"
            elif ma_late < ma_early * 0.998:
                ma_trend = "下降"
            else:
                ma_trend = "走平"
        else:
            ma_trend = "上升" if rs_latest > rs_ma else "走平"
        rs_uptrend = ma_trend == "上升" and rs_latest > rs_ma
    elif rs_latest < rs_ma * 0.995:
        ma_trend = "下降"
        rs_uptrend = False
    else:
        ma_trend = "走平"
        rs_uptrend = False

    return {
        "rs_uptrend": rs_uptrend,
        "rs_ma_trend": ma_trend,
        "rs_latest": round(rs_latest, 6),
        "rs_ma": round(rs_ma, 6),
    }


def _calc_volume_signal(volumes: List[float], period: int = 5,
                        surge_ratio: float = 1.5, shrink_ratio: float = 0.7) -> str:
    """
    成交量异动分析。

    阈值从 config/market_scoring.yaml → voting.volume 读取，允许通过回测调优。

    Args:
        surge_ratio: 量比超过此值视为放量（默认 1.5）
        shrink_ratio: 量比低于此值视为缩量（默认 0.7）
    """
    if len(volumes) < period + 1:
        return "正常"
    avg_vol = sum(volumes[-(period+1):-1]) / period
    latest = volumes[-1]
    ratio = latest / avg_vol if avg_vol > 0 else 1.0
    if ratio > surge_ratio:
        return "放量"
    elif ratio < shrink_ratio:
        return "缩量"
    return "正常"


def calc_tech_indicators(kline: List[Dict], market_mode: str = "defend") -> Dict:
    """
    计算完整技术指标（替代问财 tech_signals）

    从 K 线数据自算：
    - EMA 短期/长期交叉
    - RSI 超买超卖
    - ADX 趋势强度
    - 布林带位置

    Returns:
        {
            "ema_short": float,       # 短期 EMA（12日）
            "ema_long": float,        # 长期 EMA（26日）
            "ema_cross": str,         # "golden"（金叉）/ "dead"（死叉）/ "none"
            "rsi": float,             # RSI（0-100）
            "rsi_signal": str,        # "oversold"（超卖<30）/ "overbought"（超买>70）/ "neutral"
            "adx": float,             # ADX（0-100）
            "adx_signal": str,        # "strong"（>25）/ "weak"（<20）/ "neutral"
            "bollinger": {...},       # 布林带
            "vote": str,              # 三维投票结果："bullish"/"bearish"/"neutral"
            "vote_score": int,        # 投票得分：-3 到 +3
        }
    """
    if not kline or len(kline) < 30:
        return {}

    closes = [float(k.get("close", k.get("收盘", 0))) for k in kline]
    highs = [float(k.get("high", k.get("最高", 0))) for k in kline]
    lows = [float(k.get("low", k.get("最低", 0))) for k in kline]

    # EMA
    ema12_list = calc_ema(closes, 12)
    ema26_list = calc_ema(closes, 26)
    ema_short = ema12_list[-1]
    ema_long = ema26_list[-1]
    # 交叉判断
    if len(ema12_list) >= 2 and len(ema26_list) >= 2:
        prev_diff = ema12_list[-2] - ema26_list[-2]
        curr_diff = ema12_list[-1] - ema26_list[-1]
        if prev_diff <= 0 and curr_diff > 0:
            ema_cross = "golden"
        elif prev_diff >= 0 and curr_diff < 0:
            ema_cross = "dead"
        else:
            ema_cross = "none"
    else:
        ema_cross = "none"

# ---- 加载投票阈值配置 ----
    try:
        from ..config_models import load_config
        voting_cfg = load_config("market_scoring.yaml").get("voting", {})
    except Exception:
        voting_cfg = {}

    vol_cfg = voting_cfg.get("volume", {})
    rsi_cfg = voting_cfg.get("rsi", {})
    adx_cfg = voting_cfg.get("adx", {})

    # RSI（6 区细分，借鉴 a-stock-kline-analyzer）
    rsi = calc_rsi(closes, 14)
    rsi_signal = _rsi_zone(rsi,
                           overbought=rsi_cfg.get("overbought", 70),
                           oversold=rsi_cfg.get("oversold", 30))

    # ADX
    adx = calc_adx(highs, lows, closes, 14)
    adx_strong = adx_cfg.get("strong_trend", 25)
    adx_weak = adx_cfg.get("weak_trend", 20)
    if adx > adx_strong:
        adx_signal = "strong"
    elif adx < adx_weak:
        adx_signal = "weak"
    else:
        adx_signal = "neutral"

    # 布林带（含带宽和位置百分比）
    boll = calc_bollinger(closes, 20, 2.0)

    # MACD（6 级信号粒度）
    macd = calc_macd(closes)

    # KDJ
    kdj = calc_kdj(highs, lows, closes)

    # K 线形态（TA-Lib，含信号强度 ±100）
    kline_pats = detect_kline_patterns(kline)
    bullish_pats = [p for p in kline_pats if "看涨" in p.get("signal", "")]
    bearish_pats = [p for p in kline_pats if "看跌" in p.get("signal", "")]
    # 用 TA-Lib 信号强度判断力度
    max_bull_strength = max([abs(p.get("strength", 0)) for p in bullish_pats], default=0)
    max_bear_strength = max([abs(p.get("strength", 0)) for p in bearish_pats], default=0)
    strong_bullish = max_bull_strength >= 100
    strong_bearish = max_bear_strength >= 100


    # 成交量
    volumes = [float(k.get("volume", k.get("成交量", 0))) for k in kline]
    vol_signal = _calc_volume_signal(
        volumes,
        period=vol_cfg.get("period", 5),
        surge_ratio=vol_cfg.get("surge_ratio", 1.5),
        shrink_ratio=vol_cfg.get("shrink_ratio", 0.7),
    )
    avg_vol_5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
    vol_ratio = volumes[-1] / avg_vol_5 if avg_vol_5 > 0 else 1.0
    vol_stagnation_pct = vol_cfg.get("stagnation_pct", 0.01)

    # 波动率
    volatility = calc_volatility(closes)

    # 缠论背驰
    divergence = detect_chan_divergence(closes, highs, lows)

    # ═══════════════════════════════════════════════════════════════
    # 4 类分组投票（阈值从 config/market_scoring.yaml → voting 读取）
    # ═══════════════════════════════════════════════════════════════
    default_weights = {
        "attack":  {"trend": 1.5, "momentum": 0.7, "pattern": 1.0, "volume": 1.0},
        "defend":  {"trend": 1.0, "momentum": 1.0, "pattern": 1.0, "volume": 1.0},
        "retreat": {"trend": 0.5, "momentum": 1.3, "pattern": 1.0, "volume": 1.0},
    }
    mode_weights = voting_cfg.get("mode_weights", default_weights)
    mw = mode_weights.get(market_mode, default_weights["defend"])

    details_by_cat = {"trend": [], "momentum": [], "pattern": [], "volume": []}

    # ── 趋势组：EMA + MACD + ADX ──
    trend_bull = 0
    trend_bear = 0
    if ema_cross == "golden":
        trend_bull += 1; details_by_cat["trend"].append("EMA金叉")
    elif ema_cross == "dead":
        trend_bear += 1; details_by_cat["trend"].append("EMA死叉")

    macd_state = macd.get("state", ""); macd_hist = macd.get("hist_direction", "")
    if macd_state == "金叉" or (macd_state == "金叉延续" and "红柱扩大" in macd_hist):
        trend_bull += 1; details_by_cat["trend"].append(f"MACD{macd_state}+{macd_hist}")
    elif macd_state == "死叉" or (macd_state == "死叉延续" and "绿柱扩大" in macd_hist):
        trend_bear += 1; details_by_cat["trend"].append(f"MACD{macd_state}+{macd_hist}")
    # MACD 背驰（缠论）
    div_type = divergence.get("type", "无"); div_conf = divergence.get("confidence", "低")
    if div_type == "顶背驰" and div_conf != "低":
        trend_bear += 1; details_by_cat["trend"].append(f"MACD顶背驰({div_conf})")
    elif div_type == "底背驰" and div_conf != "低":
        trend_bull += 1; details_by_cat["trend"].append(f"MACD底背驰({div_conf})")

    if adx_signal == "strong":
        # 修复问题2.3: ADX 强势时给趋势组加票（放大趋势方向）
        # ADX > 25 表示趋势强劲，应放大已有的趋势信号
        if trend_bull > trend_bear:
            trend_bull += 1; details_by_cat["trend"].append(f"ADX{adx:.0f}趋势强劲(偏多)")
        elif trend_bear > trend_bull:
            trend_bear += 1; details_by_cat["trend"].append(f"ADX{adx:.0f}趋势强劲(偏空)")

    trend_vote = 1 if trend_bull > trend_bear else (-1 if trend_bear > trend_bull else 0)

    # ── 动量组：RSI + KDJ + 布林带 ──
    mom_bull = 0
    mom_bear = 0
    if rsi_signal in ("极度超卖", "超卖"):
        mom_bull += 1; details_by_cat["momentum"].append(f"RSI{rsi_signal}({rsi:.0f})")
    elif rsi_signal in ("极度超买", "超买"):
        mom_bear += 1; details_by_cat["momentum"].append(f"RSI{rsi_signal}({rsi:.0f})")
    # 修复问题2.1: RSI "强势"(50-70)和"弱势"(30-50)是中性区间，不应投票
    # A 股验证过的逻辑：只有超买(>70)看空、超卖(<30)看多，中间区间不投票
    elif rsi_signal == "强势":
        details_by_cat["momentum"].append(f"RSI强势({rsi:.0f})(中性不投票)")
    elif rsi_signal == "弱势":
        details_by_cat["momentum"].append(f"RSI弱势({rsi:.0f})(中性不投票)")

    kdj_sig = kdj.get("signal", "")
    if kdj_sig == "金叉" or kdj_sig == "超卖":
        mom_bull += 1; details_by_cat["momentum"].append(f"KDJ{kdj_sig}")
    elif kdj_sig == "死叉" or kdj_sig == "超买":
        mom_bear += 1; details_by_cat["momentum"].append(f"KDJ{kdj_sig}")

    if boll["position"] == "below":
        mom_bull += 1; details_by_cat["momentum"].append("布林下轨")
    elif boll["position"] == "above":
        mom_bear += 1; details_by_cat["momentum"].append("布林上轨")

    mom_vote = 1 if mom_bull > mom_bear else (-1 if mom_bear > mom_bull else 0)

    # ── 形态组：K线加权（保留现有时效衰减 + 可靠性折扣逻辑）──
    _recency_weights = {1: 1.0, 2: 0.6, 3: 0.35, 4: 0.2, 5: 0.1}
    _conf_weights = {"极高": 1.0, "高": 0.85, "中": 0.5, "低": 0.0}
    bull_weighted = 0.0; bear_weighted = 0.0
    max_bull_s = 0; max_bear_s = 0
    for p in kline_pats:
        sig = p.get("signal", "")
        pos_n = p.get("offset", 5)
        rw = _recency_weights.get(pos_n, 0.1)
        cw = _conf_weights.get(p.get("confidence", "中"), 0.5)
        abs_s = abs(p.get("strength", 0))
        if "看涨" in sig:
            bull_weighted += abs_s * rw * cw
            if abs_s > max_bull_s: max_bull_s = abs_s
        elif "看跌" in sig:
            bear_weighted += abs_s * rw * cw
            if abs_s > max_bear_s: max_bear_s = abs_s

    if bull_weighted > bear_weighted and bull_weighted >= 60:
        pat_vote = 1
        details_by_cat["pattern"].append(f"K线看涨(加权{bull_weighted:.0f},最强s={max_bull_s})")
    elif bear_weighted > bull_weighted and bear_weighted >= 60:
        pat_vote = -1
        details_by_cat["pattern"].append(f"K线看跌(加权{bear_weighted:.0f},最强s={max_bear_s})")
    elif bull_weighted > 0 or bear_weighted > 0:
        pat_vote = 0
        if bull_weighted > bear_weighted:
            details_by_cat["pattern"].append(f"K线偏涨但不足(加权{bull_weighted:.0f})")
        elif bear_weighted > bull_weighted:
            details_by_cat["pattern"].append(f"K线偏跌但不足(加权{bear_weighted:.0f})")
        else:
            details_by_cat["pattern"].append(f"K线多空拉锯(涨{bull_weighted:.0f}/跌{bear_weighted:.0f})")
    else:
        pat_vote = 0

    # ── 量能组：量价关系 ──
    vol_vote = 0
    if vol_signal == "放量":
        if len(closes) >= 2:
            chg = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0
            if chg > vol_stagnation_pct:
                vol_vote = 1; details_by_cat["volume"].append(f"放量上涨+{chg*100:.1f}%")
            elif chg < -vol_stagnation_pct:
                vol_vote = -1; details_by_cat["volume"].append(f"放量下跌{chg*100:.1f}%")
            else:
                vol_vote = -1; details_by_cat["volume"].append("放量滞涨(警惕出货)")
    elif vol_signal == "缩量":
        # 修复问题2.2: 缩量下跌改为中性（可能是惜售，也可能是无人接盘，方向不确定）
        # A 股验证过的逻辑：只有缩量上涨算看空（乏力），缩量下跌不投票
        if len(closes) >= 2 and closes[-1] < closes[-2]:
            details_by_cat["volume"].append("缩量下跌(中性)")
        else:
            vol_vote = -1; details_by_cat["volume"].append("缩量上涨(乏力)")

    # ════════════════════════════════════════════════════════
    # 类别间加权投票
    # ════════════════════════════════════════════════════════
    weighted_score = (
        trend_vote * mw["trend"]
        + mom_vote * mw["momentum"]
        + pat_vote * mw["pattern"]
        + vol_vote * mw["volume"]
    )

    # 兼容旧接口：保留 vote_score 和 vote（7 档映射适配新范围）
    # 新 score 范围约 -3.5 到 +3.5，映射到旧 7 档
    vote_score = round(weighted_score, 1)
    category_votes = {
        "trend": {"vote": trend_vote, "weight": mw["trend"], "details": details_by_cat["trend"]},
        "momentum": {"vote": mom_vote, "weight": mw["momentum"], "details": details_by_cat["momentum"]},
        "pattern": {"vote": pat_vote, "weight": mw["pattern"], "details": details_by_cat["pattern"]},
        "volume": {"vote": vol_vote, "weight": mw["volume"], "details": details_by_cat["volume"]},
    }

    if vote_score >= 2.5:
        vote = "强烈看多"
    elif vote_score >= 1.5:
        vote = "偏多"
    elif vote_score >= 0.5:
        vote = "温和偏多"
    elif vote_score <= -2.5:
        vote = "强烈看空"
    elif vote_score <= -1.5:
        vote = "偏空"
    elif vote_score <= -0.5:
        vote = "温和偏空"
    else:
        vote = "中性"

    # 构建兼容旧格式的 vote_details
    vote_details = []
    for cat_name, cat_info in category_votes.items():
        cat_label = {"trend": "趋势", "momentum": "动量", "pattern": "形态", "volume": "量能"}[cat_name]
        dir_str = {1: "看多", -1: "看空", 0: "中性"}[cat_info["vote"]]
        w = cat_info["weight"]
        detail_str = ",".join(cat_info["details"]) if cat_info["details"] else "无信号"
        vote_details.append(f"{cat_label}组:{dir_str}(×{w}) [{detail_str}]")

    return {
        "ema_short": round(ema_short, 2),
        "ema_long": round(ema_long, 2),
        "ema_cross": ema_cross,
        "rsi": round(rsi, 2),
        "rsi_signal": rsi_signal,
        "adx": round(adx, 2),
        "adx_signal": adx_signal,
        "bollinger": {k: round(v, 2) if isinstance(v, float) else v for k, v in boll.items()},
        "vote": vote,
        "vote_score": vote_score,
        "vote_details": vote_details,
        "category_votes": category_votes,
        # MACD
        "macd": macd,
        # KDJ
        "kdj": kdj,
        # 量能
        "volume_signal": vol_signal,
        "volume_ratio": round(vol_ratio, 1),
        # 波动率
        "volatility": round(volatility, 1),
        # 缠论背驰
        "chan_divergence": divergence,
    }


# ============================================================
# K 线形态识别（替代问财 kline_pattern）
# 需求 §12.2 #5：15 种 K 线形态自动检测
# ============================================================

def fetch_stock_fund_flow(code: str, retries: int = 2) -> Optional[Dict]:
    """
    获取个股主力资金流向（问财 OpenAPI，替代东财爬虫）

    数据源：同花顺问财 OpenAPI，走正规 Bearer Token 认证通道，
    无频率限制，无反爬问题。

    Returns:
        {"main_net": float,       # 主力净流入（元）
         "super_large_net": float, # 超大单净流入（元）
         "large_net": float,      # 大单净流入（元）
         "medium_net": float,     # 中单净流入（元）
         "small_net": float,      # 小单净流入（元）
         "signal": "流入"|"流出"|"平衡"} or None
    """
    import time
    last_error = None
    for attempt in range(retries + 1):
        try:
            from .iwencai_api import query_stock_fund_flow
            result = query_stock_fund_flow(code)
            if result is not None:
                return result
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    logger.debug("个股资金流查询失败 %s（重试%d次）: %s", code, retries, last_error)
    return None


def batch_fetch_stock_fund_flow(codes: List[str]) -> Dict[str, Optional[Dict]]:
    """
    批量获取多只个股的资金流向（问财 OpenAPI，无频率限制）。

    逐只查询，走正规 API 通道，不再需要 0.8s 冷却（东财限流不再适用）。

    Returns:
        {code: fetch_stock_fund_flow() dict or None}
    """
    import time
    results = {}
    for i, code in enumerate(codes):
        if i > 0:
            time.sleep(0.2)  # 轻微间隔，避免瞬时并发过高
        results[code] = fetch_stock_fund_flow(code)
    hit = sum(1 for v in results.values() if v is not None)
    logger.info("批量资金流(问财): %d/%d 命中", hit, len(codes))
    return results


# ============================================================
# K 线形态识别（基于 TA-Lib 行业标准库，替代问财 kline_pattern）
# ============================================================

# TA-Lib 形态函数 → 中文名称 + 信号方向 + 可靠度
_TALIB_PATTERN_MAP = {
    # ── 单线形态 ──
    "CDLDOJI":              ("十字星",       "变盘",   "中"),
    "CDLLONGLEGGEDDOJI":    ("长脚十字星",   "变盘",   "中"),
    "CDLDRAGONFLYDOJI":     ("蜻蜓十字星",   "看涨",   "中"),
    "CDLGRAVESTONEDOJI":    ("墓碑十字星",   "看跌",   "中"),
    "CDLSPINNINGTOP":       ("纺锤线",       "犹豫",   "低"),
    "CDLMARUBOZU":          ("光头光脚",     "趋势",   "中"),
    "CDLSHORTLINE":         ("短实体线",     "犹豫",   "低"),
    "CDLLONGLINE":          ("长实体线",     "趋势",   "中"),
    "CDLHIGHWAVE":          ("高浪线",       "变盘",   "低"),
    "CDLRICKSHAWMAN":       ("黄包车夫",     "变盘",   "低"),
    # ── 锤子/上吊类 ──
    "CDLHAMMER":            ("锤子线",       "看涨",   "中"),
    "CDLHANGINGMAN":        ("上吊线",       "看跌",   "中"),
    "CDLINVERTEDHAMMER":    ("倒锤线",       "看涨",   "低"),
    "CDLSHOOTINGSTAR":      ("射击之星",     "看跌",   "中"),
    "CDLTAKURI":            ("探水竿",       "看涨",   "低"),
    # ── 吞没/孕线类 ──
    "CDLENGULFING":         ("吞没形态",     "双向",   "高"),
    "CDLHARAMI":            ("孕线",         "双向",   "中"),
    "CDLHARAMICROSS":       ("十字孕线",     "双向",   "中"),
    "CDLCOUNTERATTACK":     ("反击线",       "双向",   "低"),
    # ── 星形态 ──
    "CDLMORNINGSTAR":       ("早晨之星",     "看涨",   "高"),
    "CDLMORNINGDOJISTAR":   ("早晨十字星",   "看涨",   "高"),
    "CDLEVENINGSTAR":       ("黄昏之星",     "看跌",   "高"),
    "CDLEVENINGDOJISTAR":   ("黄昏十字星",   "看跌",   "高"),
    "CDLDOJISTAR":          ("十字星形态",   "双向",   "中"),
    "CDLABANDONEDBABY":     ("弃婴形态",     "双向",   "高"),
    "CDL3STARSINSOUTH":     ("南方三星",     "看涨",   "低"),
    "CDLTRISTAR":           ("三星形态",     "双向",   "中"),
    # ── 三线形态 ──
    "CDL3WHITESOLDIERS":    ("红三兵",       "看涨",   "高"),
    "CDL3BLACKCROWS":       ("三只乌鸦",     "看跌",   "高"),
    "CDLIDENTICAL3CROWS":   ("三胞胎乌鸦",   "看跌",   "中"),
    "CDL3INSIDE":           ("三内升/降",    "双向",   "中"),
    "CDL3OUTSIDE":          ("三外升/降",    "双向",   "中"),
    "CDLADVANCEBLOCK":      ("前方受阻",     "看跌",   "中"),
    "CDLSTALLEDPATTERN":    ("停顿形态",     "看跌",   "低"),
    "CDL3LINESTRIKE":       ("三线打击",     "持续",   "中"),
    # ── 三法形态 ──
    "CDLRISEFALL3METHODS":  ("上升三法",     "双向",   "高"),
    "CDLXSIDEGAP3METHODS":  ("跳空三法",     "双向",   "中"),
    "CDLMATHOLD":           ("铺垫形态",     "看涨",   "低"),
    # ── 二线形态 ──
    "CDLPIERCING":          ("刺透形态",     "看涨",   "中"),
    "CDLDARKCLOUDCOVER":    ("乌云盖顶",     "看跌",   "高"),
    "CDLHOMINGPIGEON":      ("家鸽形态",     "看涨",   "低"),
    "CDLMEETINGLINES":      ("约会线",       "双向",   "低"),
    "CDLMATCHINGLOW":       ("匹配低点",     "看涨",   "低"),
    "CDLONNECK":            ("颈上线",       "看跌",   "低"),
    "CDLINNECK":            ("颈内线",       "看跌",   "低"),
    "CDLTHRUSTING":         ("插入线",       "看跌",   "低"),
    "CDLSEPARATINGLINES":   ("分离线",       "持续",   "低"),
    # ── 缺口形态 ──
    "CDLGAPSIDESIDEWHITE":  ("缺口并列阳线", "看涨",   "中"),
    "CDLUPSIDEGAP2CROWS":   ("缺口双鸦",     "看跌",   "中"),
    "CDLTASUKIGAP":         ("跳空缺口",     "双向",   "中"),
    # ── 夹心/三明治形态 ──
    "CDLSTICKSANDWICH":     ("条形三明治",   "看涨",   "低"),
    "CDLUNIQUE3RIVER":      ("独特三河",     "看涨",   "低"),
    "CDLLADDERBOTTOM":      ("梯底形态",     "看涨",   "中"),
    "CDLCONCEALBABYSWALL":  ("藏婴吞没",     "看跌",   "中"),
    # ── 腰带/开盘收盘形态 ──
    "CDLBELTHOLD":          ("捉腰带线",     "双向",   "低"),
    "CDLCLOSINGMARUBOZU":   ("收盘光头光脚", "趋势",   "中"),
    "CDLOPENINGMARUBOZU":   ("开盘光头光脚", "趋势",   "低"),
    "CDLBREAKAWAY":         ("突破形态",     "双向",   "中"),
    # ── 其他 ──
    "CDL2CROWS":            ("双鸦",         "看跌",   "中"),
    "CDLKICKING":           ("反冲形态",     "双向",   "中"),
    "CDLKICKINGBYLENGTH":   ("长反冲形态",   "双向",   "低"),
    "CDLHIKKAKE":           ("陷阱形态",     "双向",   "低"),
    "CDLHIKKAKEMOD":        ("修正陷阱",     "双向",   "低"),
}


def detect_kline_patterns(kline: List[Dict]) -> List[Dict]:
    """
    基于 TA-Lib 行业标准库识别 K 线形态（60+ 种，带信号强度）

    Returns:
        [{"pattern": "锤子线", "signal": "看涨反转", "confidence": "高", "strength": 100}, ...]
    """
    if len(kline) < 5:
        return []

    try:
        import talib
        import numpy as np
    except ImportError:
        # TA-Lib 未安装时降级到空（外部有问财兜底）
        return []

    # 提取 OHLC 数组
    opens = np.array([float(k.get("open", k.get("开盘", 0))) for k in kline])
    highs = np.array([float(k.get("high", k.get("最高", 0))) for k in kline])
    lows = np.array([float(k.get("low", k.get("最低", 0))) for k in kline])
    closes = np.array([float(k.get("close", k.get("收盘", 0))) for k in kline])

    detected = []

    for func_name, (cn_name, direction, reliability) in _TALIB_PATTERN_MAP.items():
        func = getattr(talib, func_name, None)
        if func is None:
            continue
        try:
            result = func(opens, highs, lows, closes)
        except Exception:
            continue

        # 扫描最近 5 根 K 线
        for offset in range(-min(5, len(result)), 0):
            signal_val = int(result[offset])
            if signal_val == 0:
                continue

            # 确定实际方向
            if direction == "双向":
                actual_dir = "看涨" if signal_val > 0 else "看跌"
                sig_text = f"{actual_dir}反转" if abs(signal_val) >= 80 else actual_dir
            elif direction == "持续":
                sig_text = "看涨持续" if signal_val > 0 else "看跌持续"
            elif direction == "趋势":
                sig_text = "看涨趋势" if signal_val > 0 else "看跌趋势"
            elif direction == "变盘":
                sig_text = "变盘信号"
            elif direction == "犹豫":
                sig_text = "方向犹豫"
            else:
                sig_text = f"{direction}反转" if abs(signal_val) >= 80 else direction

            # 强度 → 置信度映射
            abs_val = abs(signal_val)
            if abs_val >= 120:
                conf = "极高"
            elif abs_val >= 100:
                conf = "高"
            elif abs_val >= 80:
                conf = "中"
            else:
                conf = "低"

            detected.append({
                "pattern": cn_name,
                "signal": sig_text,
                "confidence": reliability if abs_val >= 80 else conf,
                "strength": signal_val,
                "position": f"倒数第{abs(offset)}根K线",
                "offset": abs(offset),  # 数字，1=最近一根，供时效加权用
            })

    # ═══════════════════════════════════════════════════════
    # 噪声过滤（3 层）：
    # ① 去掉无方向形态（趋势/犹豫/变盘 = 形状描述，不是交易信号）
    # ② 去掉低置信度形态（strength<80 且内置可靠度≠高）
    # ③ 去重 + 截断（最多保留 6 个）
    # ═══════════════════════════════════════════════════════
    _noise_directions = {"趋势", "犹豫", "变盘"}
    filtered = []
    for d in detected:
        # 层①：纯形状描述，跳过
        sig = d.get("signal", "")
        if any(nd in sig for nd in ["趋势", "方向犹豫", "变盘信号"]):
            continue
        # 层②：低置信度跳过（除非 TA-Lib 内置可靠度为"高"）
        if d.get("confidence") == "低":
            continue
        filtered.append(d)

    # 按强度降序
    filtered.sort(key=lambda x: abs(x["strength"]), reverse=True)

    # 去重：同一形态只保留最强的
    seen = set()
    unique = []
    for d in filtered:
        if d["pattern"] not in seen:
            seen.add(d["pattern"])
            unique.append(d)

    # 层③：最多 6 个形态，优先保留高置信度
    return unique[:6]


def detect_support_resistance(kline: List[Dict], window: int = 20) -> Dict:
    """
    识别关键支撑位和阻力位（移植自 wuritu-stock-technical-analysis）

    基于：前期高低点、密集成交区、整数关口
    """
    if len(kline) < window + 1:
        return {"supports": [], "resistances": [], "current_price": 0}

    closes = [float(k.get("close", k.get("收盘", 0))) for k in kline]
    highs = [float(k.get("high", k.get("最高", 0))) for k in kline]
    lows = [float(k.get("low", k.get("最低", 0))) for k in kline]
    current_price = closes[-1]

    # 前期高低点
    recent_highs = []
    recent_lows = []
    for i in range(window, len(kline) - 1):
        if highs[i] == max(highs[i - window:i + window + 1]):
            recent_highs.append(round(highs[i], 2))
        if lows[i] == min(lows[i - window:i + window + 1]):
            recent_lows.append(round(lows[i], 2))

    # 支撑位（当前价下方的前期低点）
    supports = sorted(set(
        [p for p in recent_lows if p < current_price]
    ), reverse=True)[:3]

    # 阻力位（当前价上方的前期高点）
    resistances = sorted(set(
        [p for p in recent_highs if p > current_price]
    ))[:3]

    # 整数关口
    base = round(current_price, -1 if current_price > 100 else 0)
    round_levels = [base - 10, base, base + 10] if current_price > 100 else [
        base - 1, base, base + 1]

    return {
        "current_price": round(current_price, 2),
        "supports": supports,
        "resistances": resistances,
        "round_levels": round_levels,
    }


# ============================================================
# 统一行情预取器（AKShare优先 → 问财降级）
# 供 StockFilter / PositionAnalyzer 共用
# 消除各模块各自维护 _quote_cache 的重复（优化 #3）
# ============================================================

def _safe_float_extract(data: Dict, keys: List[str], default=None) -> Optional[float]:
    """从字典中按多个可能的字段名安全提取浮点值"""
    for key in keys:
        val = data.get(key)
        if val is not None:
            try:
                if isinstance(val, str):
                    val = val.replace("%", "").replace(",", "").strip()
                return float(val)
            except (ValueError, TypeError):
                continue
    return default


def prefetch_quotes(codes: List[str]) -> Dict[str, Optional[Dict]]:
    """
    统一批量预取行情（AKShare优先 → 问财降级）

    策略：
    1. AKShare 全市场实时行情（0 问财配额，一次拉 5500+ 只）
    2. 问财 batch_query_stock_quotes 补充 AKShare 未命中的标的

    Args:
        codes: 股票代码列表

    Returns:
        {code: {current_price, change_pct, name, is_st, is_suspended, source} | None}
    """
    if not codes:
        return {}

    today = datetime.now().strftime("%Y-%m-%d")
    results: Dict[str, Optional[Dict]] = {}

    # ---- 1. AKShare 全市场行情（主源，0 问财配额） ----
    ak_results = batch_get_realtime_quotes(codes)
    for code in codes:
        if code in ak_results and ak_results.get(code):
            q = ak_results[code]
            results[code] = {
                "current_price": q.get("current_price", 0),
                "change_pct": q.get("change_pct", 0),
                "name": q.get("name", ""),
                "is_st": q.get("is_st", False),
                "is_suspended": q.get("is_suspended", False),
                "source": "akshare",
            }

    # ---- 2. 问财补充 AKShare 未命中的（降级） ----
    missed = [c for c in codes if c not in results]
    if missed:
        try:
            from .skill_wrapper import get_skill_wrapper
            sw = get_skill_wrapper()
            wc_results = sw.batch_query_stock_quotes(missed)
            for code, result in wc_results.items():
                if result and result.get("data"):
                    raw = result["data"]
                    if isinstance(raw, list) and raw:
                        raw = raw[0] if isinstance(raw[0], dict) else {}
                    if isinstance(raw, dict):
                        name = raw.get("name", raw.get("股票简称", ""))
                        results[code] = {
                            "current_price": _safe_float_extract(
                                raw, ["最新价", "收盘价", "现价"], 0,
                            ),
                            "change_pct": _safe_float_extract(
                                raw, ["最新涨跌幅", "最新涨跌幅:前复权", "涨跌幅"], 0,
                            ),
                            "name": name,
                            "is_st": raw.get("is_st", "ST" in str(name)),
                            "is_suspended": raw.get("is_suspended", False),
                            "source": "iwencai",
                        }
        except Exception as e:
            logger.warning("问财补充行情失败（跳过降级）: %s", e)

    # ---- 更新模块级缓存 ----
    global _quote_prefetch_cache, _quote_prefetch_date
    _quote_prefetch_cache = results
    _quote_prefetch_date = today

    ak_hit = sum(1 for v in results.values() if v and v.get("source") == "akshare")
    wc_hit = sum(1 for v in results.values() if v and v.get("source") == "iwencai")
    logger.info(
        "行情预取: 请求 %d 只, 命中 %d 只（AKShare %d + 问财 %d）",
        len(codes), ak_hit + wc_hit, ak_hit, wc_hit,
    )
    return results


def get_prefetched_quote(code: str) -> Optional[Dict]:
    """获取单只股票实时行情（无缓存，直接拉取）"""
    try:
        quotes = batch_get_realtime_quotes([code])
        return quotes.get(code)
    except Exception:
        return None


if __name__ == "__main__":
    # 测试
    print("=== 测试批量实时行情 ===")
    quotes = batch_get_realtime_quotes(["688256", "000001", "600519"])
    for code, q in quotes.items():
        print(f"  {code}({q['name']}): 价{q['current_price']:.2f} 涨跌{q['change_pct']:.2f}% ST={q['is_st']}")

    print()
    print("=== 测试技术指标（用模拟数据）===")
    import random
    random.seed(42)
    mock_kline = []
    price = 100.0
    for i in range(60):
        open_p = price
        close_p = price * (1 + random.uniform(-0.03, 0.03))
        high = max(open_p, close_p) * (1 + random.uniform(0, 0.02))
        low = min(open_p, close_p) * (1 - random.uniform(0, 0.02))
        mock_kline.append({"open": open_p, "close": close_p, "high": high, "low": low})
        price = close_p

    tech = calc_tech_indicators(mock_kline)
    print(f"  EMA: short={tech['ema_short']:.2f} long={tech['ema_long']:.2f} cross={tech['ema_cross']}")
    print(f"  RSI: {tech['rsi']:.2f} ({tech['rsi_signal']})")
    print(f"  ADX: {tech['adx']:.2f} ({tech['adx_signal']})")
    print(f"  布林带: position={tech['bollinger']['position']}")
    print(f"  投票: {tech['vote']} (score={tech['vote_score']})")

    print()
    print("=== 测试 K 线形态 ===")
    # 模拟一个十字星
    mock_kline[-1] = {"open": 100.0, "close": 100.1, "high": 102.0, "low": 98.0}
    patterns = detect_kline_patterns(mock_kline)
    for p in patterns:
        print(f"  {p['pattern']}: {p['signal']} (置信度={p['confidence']})")
