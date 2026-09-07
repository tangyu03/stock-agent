"""
F10: 龙虎榜评分模型
===================
数据源：ak.stock_lhb_detail_em + ak.stock_lhb_jgmmtj_em
评分维度（百分制）：
  1. 机构净买：+30分（机构净买额>0 且 占总成交比>5%）
  2. 买方集中度：+20分（买方机构数>卖方机构数 且 买方机构数>=3）
  3. 净买额占总成交比：+20分（>5%满分，线性递减）
  4. 上榜后历史涨跌：+30分（近30日上榜后5日平均涨跌>0）
  5. 卖方黑名单：-25分（解读含"实力游资卖出"等关键词）

输出：
  score: 0-100
  label: "强烈看多(>=70)/看多(50-70)/中性(30-50)/看空(<30)"
  detail: 评分明细

使用方式：
  from src.analyzers.lhb_scorer import score_lhb
  result = score_lhb("688008")
"""
import logging
from typing import Dict, Any
from datetime import datetime, timedelta

# 修复 BUG-L1(2026-08-27): P2-3 迁移到统一 SessionCache 时漏掉了导入，
# 原 get_session_cache() 调用会抛 NameError 且发生在 try 块之外 → score_lhb 直接崩溃。
from ..utils.session_cache import get_session_cache

logger = logging.getLogger(__name__)

# 席位黑名单关键词（解读字段含这些词则扣分）
_SEAT_BLACKLIST_KEYWORDS = [
    "实力游资卖出", "游资出货", "机构卖出", "机构出货",
    "成功率低于", "亏损", "被套",
]


def _get_lhb_data(target_date: str = "") -> Dict:
    """获取龙虎榜数据（P2-3: 改用统一SessionCache）"""
    cache = get_session_cache()
    today = datetime.now().strftime("%Y%m%d")

    def _fetch():
        try:
            from ..data_layer.akshare_safe import safe_ak_func
            end_date = today
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            lhb_detail = safe_ak_func("stock_lhb_detail_em", timeout=30)
            lhb_jgmmtj = safe_ak_func("stock_lhb_jgmmtj_em", timeout=30)
            detail_df = lhb_detail(start_date=start_date, end_date=end_date)
            jgmmtj_df = lhb_jgmmtj(start_date=start_date, end_date=end_date)
            logger.info("龙虎榜数据加载: 明细%d行, 机构统计%d行",
                        len(detail_df) if detail_df is not None else 0,
                        len(jgmmtj_df) if jgmmtj_df is not None else 0)
            return {"detail": detail_df, "jgmmtj": jgmmtj_df}
        except Exception as e:
            logger.warning("龙虎榜数据加载失败: %s", e)
            return None

    result = cache.get_or_fetch(f"lhb_data:{today}", _fetch, ttl=3600)
    if result is None:
        return {"detail": None, "jgmmtj": None}
    return result


def score_lhb(stock_code: str) -> Dict[str, Any]:
    """
    龙虎榜百分制评分

    Args:
        stock_code: 股票代码

    Returns:
        {
            "score": 0-100,
            "label": "强烈看多/看多/中性/看空",
            "detail": str,
            "raw": {...},
            "stale": bool,  # 是否无龙虎榜数据
        }
    """
    data = _get_lhb_data()
    detail_df = data["detail"]
    jgmmtj_df = data["jgmmtj"]

    if detail_df is None or detail_df.empty:
        return {"score": 50, "label": "中性", "detail": "无龙虎榜数据", "raw": {}, "stale": True}

    # 找该股票的龙虎榜记录
    code_col = "代码" if "代码" in detail_df.columns else detail_df.columns[1]
    stock_records = detail_df[detail_df[code_col].astype(str).str.zfill(6) == stock_code]

    if stock_records.empty:
        return {"score": 50, "label": "中性", "detail": "近期未上榜", "raw": {}, "stale": True}

    # 取最近一次上榜
    latest = stock_records.iloc[0]
    score = 50  # 基础分
    details = []

    # 1. 机构净买（+30分）
    net_buy = float(latest.get("龙虎榜净买额", 0) or 0)
    total_volume = float(latest.get("市场总成交额", 0) or 0)
    net_buy_ratio = net_buy / total_volume if total_volume > 0 else 0

    # 从机构统计找该股票
    inst_net_buy = 0
    buyer_inst = 0
    seller_inst = 0
    if jgmmtj_df is not None and not jgmmtj_df.empty:
        jg_code_col = "代码" if "代码" in jgmmtj_df.columns else jgmmtj_df.columns[1]
        jg_records = jgmmtj_df[jgmmtj_df[jg_code_col].astype(str).str.zfill(6) == stock_code]
        if not jg_records.empty:
            jg_latest = jg_records.iloc[0]
            inst_net_buy = float(jg_latest.get("机构买入净额", 0) or 0)
            buyer_inst = int(jg_latest.get("买方机构数", 0) or 0)
            seller_inst = int(jg_latest.get("卖方机构数", 0) or 0)

    if inst_net_buy > 0:
        inst_ratio = inst_net_buy / total_volume if total_volume > 0 else 0
        inst_score = min(30, inst_ratio * 100 * 6)  # 5%占比=30分满分
        score += inst_score
        details.append(f"机构净买{inst_net_buy/1e8:.2f}亿(占{inst_ratio*100:.1f}%)+{inst_score:.0f}分")

    # 2. 买方集中度（+20分）
    if buyer_inst >= 3 and buyer_inst > seller_inst:
        score += 20
        details.append(f"买方机构{buyer_inst}>卖方{seller_inst}+20分")
    elif buyer_inst > seller_inst:
        score += 10
        details.append(f"买方机构{buyer_inst}>卖方{seller_inst}+10分")

    # 3. 净买额占总成交比（+20分）
    if net_buy_ratio > 0.05:
        score += 20
        details.append(f"净买占总成交{net_buy_ratio*100:.1f}%+20分")
    elif net_buy_ratio > 0.02:
        score += 10
        details.append(f"净买占总成交{net_buy_ratio*100:.1f}%+10分")

    # 4. 上榜后历史涨跌（+30分）— 取近30天所有上榜后5日涨跌均值
    if "上榜后5日" in stock_records.columns:
        post_5d_returns = []
        for _, r in stock_records.iterrows():
            ret = r.get("上榜后5日")
            if ret is not None and str(ret) not in ["", "None", "nan"]:
                try:
                    post_5d_returns.append(float(ret))
                except (ValueError, TypeError):
                    pass
        if post_5d_returns:
            avg_5d = sum(post_5d_returns) / len(post_5d_returns)
            if avg_5d > 3:
                score += 30
                details.append(f"上榜后5日均值{avg_5d:.1f}%+30分")
            elif avg_5d > 0:
                score += 15
                details.append(f"上榜后5日均值{avg_5d:.1f}%+15分")
            elif avg_5d < -3:
                score -= 15
                details.append(f"上榜后5日均值{avg_5d:.1f}%-15分")

    # 5. 卖方黑名单（-25分）
    interpretation = str(latest.get("解读", "") or "")
    blacklist_hit = [kw for kw in _SEAT_BLACKLIST_KEYWORDS if kw in interpretation]
    if blacklist_hit:
        score -= 25
        details.append(f"黑名单关键词[{','.join(blacklist_hit)}]-25分")

    score = max(0, min(100, score))

    # 标签
    if score >= 70:
        label = "强烈看多"
    elif score >= 50:
        label = "看多"
    elif score >= 30:
        label = "中性"
    else:
        label = "看空"

    return {
        "score": round(score, 1),
        "label": label,
        "detail": " | ".join(details) if details else "无显著信号",
        "raw": {
            "net_buy": net_buy,
            "net_buy_ratio": net_buy_ratio,
            "inst_net_buy": inst_net_buy,
            "buyer_inst": buyer_inst,
            "seller_inst": seller_inst,
            "interpretation": interpretation,
            "blacklist_hit": blacklist_hit,
        },
        "stale": False,
    }


def get_lhb_score_for_sector(stock_codes: list) -> Dict[str, Any]:
    """批量龙虎榜评分"""
    scores = []
    for code in stock_codes:
        result = score_lhb(code)
        if not result["stale"]:
            scores.append(result)
    if not scores:
        return {"avg_score": 50, "bullish_count": 0, "bearish_count": 0, "detail": "无龙虎榜数据"}
    avg = sum(s["score"] for s in scores) / len(scores)
    bullish = sum(1 for s in scores if s["score"] >= 50)
    bearish = sum(1 for s in scores if s["score"] < 30)
    return {
        "avg_score": round(avg, 1),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "detail": f"龙虎榜均分{avg:.1f}, 看多{bullish}/看空{bearish}",
    }
