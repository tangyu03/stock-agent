"""
消息模板渲染 - 完整决策卡片版
每个信号展示完整决策链：环境->技术->形态->过滤->信号逻辑->风控
"""
from typing import Dict, Any, List, Optional, Tuple

def _pct(v: float, digits: int = 1) -> str:
    return f"+{v*100:.{digits}f}%" if v >= 0 else f"{v*100:.{digits}f}%"

def _val(v, digits: int = 2) -> str:
    if v is None: return "N/A"
    try: return f"{float(v):.{digits}f}"
    except (TypeError, ValueError): return str(v)

def _fund_amount(amount: float) -> str:
    """格式化资金流向金额（元→亿/万），如 1.50亿流入, -3200万流出"""
    if amount is None:
        return ""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return ""
    if amt == 0:
        return ""
    abs_amt = abs(amt)
    direction = "流入" if amt > 0 else "流出"
    if abs_amt >= 100_000_000:  # >= 1亿
        return f"主力{direction}{abs_amt/100_000_000:.2f}亿"
    else:
        return f"主力{direction}{abs_amt/10000:.0f}万"


def _sector_label(s: str) -> str:
    """板块状态英文→中文"""
    return {"main_trend": "主线", "rotational": "轮动", "retreating": "退潮",
            "unknown": "未知", "主线": "主线", "支线": "支线", "退潮": "退潮"}.get(s, s or "N/A")


def _institutional(data) -> str:
    """
    渲染机构持仓打分（4 数据源投票 + 具体数值）。

    数据来自 tech_data['institutional_holding']，包含：
    - vote_score: 总票数 (-4 到 +4)
    - vote_label: 机构看多/看空/中性
    - votes: 各数据源详情 (north_bound/lhb/main_force/shareholder)
    """
    inst = data.get("institutional_holding")
    if not inst or not isinstance(inst, dict):
        return ""

    parts = []
    score = inst.get("vote_score", 0)
    label = inst.get("vote_label", "机构中性")
    bull = inst.get("bullish_count", 0)
    bear = inst.get("bearish_count", 0)

    # 总分 + 标签
    emoji = "🟢" if score >= 2 else ("🔴" if score <= -2 else "⚪")
    parts.append(f"{emoji}{label}({score:+d}票,多{bull}/空{bear})")

    # 各数据源具体数值
    votes = inst.get("votes", {})
    if votes:
        detail_parts = []
        for src_name, src_data in votes.items():
            if not isinstance(src_data, dict):
                continue
            v = src_data.get("vote", 0)
            detail = src_data.get("detail", "")
            if not detail:
                continue
            # 简化数据源名称
            short_name = {
                "north_bound": "北向",
                "lhb": "龙虎榜",
                "main_force": "主力",
                "shareholder": "股东",
            }.get(src_name, src_name)
            arrow = "↑" if v > 0 else ("↓" if v < 0 else "→")
            detail_parts.append(f"{short_name}{arrow}{detail}")
        if detail_parts:
            parts.append(" | ".join(detail_parts))

    return " | ".join(parts)

def _tech(data):
    parts = []
    ma5, ma10, ma20 = data.get("ma5"), data.get("ma10"), data.get("ma20")
    if ma5 and ma10 and ma20:
        order = "多头排列" if ma5 > ma10 > ma20 else "空头排列" if ma5 < ma10 < ma20 else "交叉震荡"
        parts.append(f"MA:{order}({_val(ma5)}/{_val(ma10)}/{_val(ma20)})")
    vr = data.get("volume_ratio")
    if vr:
        if vr < 1.0:
            label = "缩量"
        elif vr > 1.2:
            label = "放量"
        else:
            label = "量平"
        parts.append(f"量比:{vr:.2f}x{label}")
    ts = data.get("tech_signals",{})
    if ts:
        ema = ts.get("ema_cross","")
        ema_short = ts.get("ema_short")
        ema_long = ts.get("ema_long")
        if ema == "golden":
            ema_label = "金叉↑"
        elif ema == "dead":
            ema_label = "死叉↓"
        elif ema_short is not None and ema_long is not None:
            # 无交叉时显示当前排列方向
            if ema_short > ema_long:
                ema_label = f"多头({_val(ema_short)}>{_val(ema_long)})"
            elif ema_short < ema_long:
                ema_label = f"空头({_val(ema_short)}<{_val(ema_long)})"
            else:
                ema_label = "粘合"
        else:
            ema_label = "-"
        parts.append(f"EMA:{ema_label}")
        rsi_val = data.get("rsi") or ts.get("rsi")
        if rsi_val:
            zone = ts.get("rsi_signal", "")
            parts.append(f"RSI:{rsi_val:.1f}({zone})" if zone else f"RSI:{rsi_val:.1f}")
        adx = ts.get("adx")
        if adx: parts.append(f"ADX:{adx:.1f}({'趋势强' if adx>25 else '趋势弱' if adx<20 else '中性'})")
        bp = ts.get("bollinger",{}).get("position","")
        boll_label = {'above':'上轨','below':'下轨','in':'中轨'}.get(bp,'')
        if bp: parts.append(f"布林:{boll_label}")
        vote = ts.get("vote", "中性")
        sc = ts.get("vote_score", 0)
        vd = ts.get("vote_details", [])
        if vd:
            # 显示前 5 条投票明细，让推导过程透明
            top_details = " ".join(vd[:5])
            parts.append(f"投票:{vote}({sc:+.1f}) ⓘ {top_details}")
        else:
            parts.append(f"投票:{vote}({sc:+.1f})")
    ex = []
    if data.get("shrinking_pullback"): ex.append("缩量回踩")
    if data.get("pair_bottom"): ex.append("对手盘底")
    if data.get("market_near_support"): ex.append("大盘近支撑")
    if ex: parts.append("特殊:" + "|".join(ex))
    return "<br/>&nbsp;&nbsp;".join(parts) if parts else "N/A"

def _kline(data):
    pats = data.get("kline_pattern",[])
    if not pats: return "无显著形态"
    # 只保留有明确方向的形态，过滤纯形状描述
    bll = [p for p in pats if "看涨" in p.get("signal","") or "支撑" in p.get("signal","")]
    ber = [p for p in pats if "看跌" in p.get("signal","") or "压力" in p.get("signal","")]
    lines = []
    if bll: lines.append(" ↑ " + " ".join(f"{p['pattern']}({p['confidence']})" for p in bll[:4]))
    if ber: lines.append(" ↓ " + " ".join(f"{p['pattern']}({p['confidence']})" for p in ber[:4]))
    return "<br/>&nbsp;&nbsp;".join(lines) if lines else "无显著形态"

def _filter(data):
    checks = data.get("filter_checks",{})
    if not checks: return ""
    lines = []
    for ck, cv in checks.items():
        p = cv.get("passed",True)
        d = cv.get("detail","")
        lines.append("&nbsp;&nbsp;" + ("OK " if p else "X ") + ck + (f" ({d})" if d else ""))
    return "<br/>".join(lines)

def render_entry_signal(data):
    name, code = data.get("stock_name",""), data.get("stock_code","")
    et = data.get("entry_type","")
    trigger = data.get("trigger_price",0)
    stop_loss = data.get("stop_loss",0)
    target = data.get("target_range",[0,0])
    position = data.get("position_level","正常")
    sector_status = data.get("sector_status","")
    confidence = data.get("confidence","中")
    market_mode = data.get("market_mode","")
    note = data.get("note","") or data.get("trigger_reason","")
    target_type = data.get("target_type","")
    tl = {"套利低吸":"套利","恐慌抄底":"恐慌","确认追强":"追强"}.get(et, et)
    title = f"买入{tl} {name}({code})"
    content = f"<b>买入 {et} {name}({code})</b><br/><br/>"
    # 当前价格 + 涨跌幅
    cp = data.get("current_price")
    chg = data.get("change_pct")
    if cp:
        price_line = f"<b>现价</b>: {cp:.2f}"
        if chg is not None:
            try:
                chg_val = float(chg)
                emoji = "🔴" if chg_val < 0 else ("🟢" if chg_val > 0 else "")
                price_line += f" {emoji}{chg_val:+.2f}%"
            except (ValueError, TypeError):
                pass
        content += f"{price_line}<br/><br/>"
    env_parts = []
    if market_mode: env_parts.append(f"市场:{'进攻' if market_mode=='attack' else '防守' if market_mode=='defend' else '撤退'}")

    # 板块显示：名称(状态)，概念有独立状态时追加
    sector_name = data.get("sector_name","") or data.get("sw_level2","")
    sw2 = data.get("sw_level2","")
    concepts = data.get("concepts","")
    concept_status = data.get("concept_status","")

    if sector_name:
        # 区分显示：如果 sw_level2 与 sector_name 不同，说明 sector_name 是 Level1，补充显示 Level2
        if sw2 and sw2 != sector_name:
            sector_display = f"{sector_name}→{sw2}({_sector_label(sector_status)})"
        else:
            sector_display = f"{sector_name}({_sector_label(sector_status)})"
    else:
        sector_display = f"{_sector_label(sector_status)}"

    if concepts and concept_status:
        sector_display += f" | 概念:{concepts}({concept_status})"
    elif concepts:
        sector_display += f" | 概念:{concepts}"

    env_parts.append(f"板块:{sector_display}")
    content += f"<b>环境</b><br/>&nbsp;&nbsp;{' | '.join(env_parts)}<br/><br/>"
    content += f"<b>技术面</b><br/>&nbsp;&nbsp;{_tech(data)}<br/><br/>"
    # 机构持仓打分（4 数据源投票 + 具体数值）
    inst_display = _institutional(data)
    if inst_display:
        content += f"<b>机构资金</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
    content += f"<b>K线形态</b><br/>&nbsp;&nbsp;{_kline(data)}<br/><br/>"
    fc = data.get("filter_checks",{})
    if fc:
        total, passed = len(fc), sum(1 for c in fc.values() if c.get("passed",True))
        content += f"<b>前置过滤({passed}/{total})</b><br/>{_filter(data)}<br/><br/>"
    content += f"<b>信号逻辑</b><br/>&nbsp;&nbsp;类型:{et}<br/>"
    ssum = data.get("strategy_summary","")
    if ssum: content += f"&nbsp;&nbsp;策略:{ssum}<br/>"
    if note: content += f"&nbsp;&nbsp;理由:{note.replace(chr(10), '<br/>&nbsp;&nbsp;')}<br/>"
    content += f"&nbsp;&nbsp;置信度:{confidence}<br/>"
    tc = {"冲高止盈":"冲高即走","持有观察":"持有观察","主升持有":"中线持有"}.get(target_type,target_type)
    content += f"&nbsp;&nbsp;策略:{tc}<br/><br/>"
    content += f"<b>风控参数</b><br/>&nbsp;&nbsp;触发价:{_val(trigger)}<br/>"
    slr = data.get("stop_loss_reason","")
    content += f"&nbsp;&nbsp;止损:{_val(stop_loss)}" + (f"({slr})" if slr else "") + "<br/>"
    if len(target)>=2 and target[0]>0: content += f"&nbsp;&nbsp;止盈:{_val(target[0])}-{_val(target[1])}<br/>"
    content += f"&nbsp;&nbsp;仓位等级:{position}<br/>"
    if et=="套利低吸": content += "<br/><b>冲高即走不恋战</b>"
    elif et=="恐慌抄底": content += "<br/><b>中线持有仓位可偏重</b>"
    elif et=="确认追强": content += "<br/><b>去弱留强</b>"
    return title, content

def render_entry_signals_batch(signals):
    if not signals: return "", ""
    title = f"买入信号 {len(signals)}条"
    content = f"<b>买入信号({len(signals)}条)</b><br/><br/>"
    for i, s in enumerate(signals):
        _, card = render_entry_signal(s)
        if i < len(signals)-1: card += "<br/><hr/>"
        content += card
    return title, content

def render_exit_signal(data):
    name, code = data.get("stock_name",""), data.get("stock_code","")
    et = data.get("exit_type","")
    trigger = data.get("trigger_price",0)
    stop_loss = data.get("stop_loss_price",0)
    reason = data.get("reason","")
    shares = data.get("holding_shares",0)
    pnl = data.get("pnl_ratio",0)
    urgency = data.get("urgency","重要")
    market_mode = data.get("market_mode","")
    sector_status = data.get("sector_status","")
    holding_rating = data.get("holding_rating","")
    trend_status = data.get("trend_status","")
    fund_flow = data.get("fund_flow","")
    is_urgent = et == "破位止损" or urgency == "紧急"
    note = data.get("note","")

    # 观察股（无 exit_type，有 note 字段）— 用"观察"标题，不显示退出逻辑
    is_observation = not et and bool(note)
    if is_observation:
        title = f"观察 {name}({code})"
        content = f"<b>📋 观察 {name}({code})</b><br/><br/>"
    else:
        title = f"{'紧急' if is_urgent else '卖出'}{et} {name}({code})"
        content = f"<b>{'紧急' if is_urgent else '卖出'} {et} {name}({code})</b><br/><br/>"
    pnl_str = _pct(pnl) if pnl else ""
    # 当前价格 + 涨跌幅
    cp = data.get("current_price")
    chg = data.get("change_pct")
    if cp:
        price_line = f"<b>现价</b>: {cp:.2f}"
        if chg is not None:
            try:
                chg_val = float(chg)
                emoji = "🔴" if chg_val < 0 else ("🟢" if chg_val > 0 else "")
                price_line += f" {emoji}{chg_val:+.2f}%"
            except (ValueError, TypeError):
                pass
        content += f"{price_line}<br/><br/>"
    env_parts = []
    if market_mode: env_parts.append(f"市场:{'进攻' if market_mode=='attack' else '防守' if market_mode=='defend' else '撤退'}")
    sector_name = data.get("sector_name","")
    sn = f"{sector_name}:" if sector_name else ""
    sw2 = data.get("sw_level2","")
    sw3 = data.get("sw_level3","")
    concepts = data.get("concepts","")
    extra = ""
    if sw2: extra += f" | 2级:{sw2}"
    if sw3: extra += f" | 3级:{sw3}"
    if concepts: extra += f" | 概念:{concepts}"
    concept_status = data.get("concept_status","")
    if concept_status: extra += f"({concept_status})"
    env_parts.append(f"板块:{sn}{_sector_label(sector_status)}{extra}")
    if holding_rating:
        rd = data.get("rating_detail", "")
        env_parts.append(f"健康:{holding_rating}" + (f"({rd})" if rd else ""))
    content += f"<b>持仓状态</b><br/>&nbsp;&nbsp;{' | '.join(env_parts)}<br/>"
    # 资金流向详情
    fund_amount = data.get("fund_flow_amount", 0)
    fund_amount_str = _fund_amount(fund_amount)
    if fund_amount_str:
        content += f"&nbsp;&nbsp;{fund_amount_str}<br/>"
    if shares>0: content += f"&nbsp;&nbsp;持仓:{shares}股 | 浮盈:{pnl_str}<br/>"
    content += "<br/>"

    # 观察股显示板块状态 + 技术面 + 机构资金 + 说明（不显示退出逻辑/风控参数）
    if is_observation:
        tech_display = _tech(data)
        if tech_display:
            content += f"<b>技术面</b><br/>&nbsp;&nbsp;{tech_display}<br/><br/>"
        inst_display = _institutional(data)
        if inst_display:
            content += f"<b>机构资金</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
        content += f"<b>说明</b><br/>&nbsp;&nbsp;{note}<br/>"
        return title, content

    # 以下仅卖出信号显示
    content += f"<b>技术面</b><br/>&nbsp;&nbsp;{_tech(data)}<br/><br/>"
    # 机构持仓打分（4 数据源投票 + 具体数值）
    inst_display = _institutional(data)
    if inst_display:
        content += f"<b>机构资金</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
    bear = [p for p in data.get("kline_pattern",[]) if "看跌" in p.get("signal","") or "压力" in p.get("signal","")]
    if bear:
        content += f"<b>看跌形态</b><br/>&nbsp;&nbsp;"
        content += " ".join(f"{p['pattern']}({p['confidence']})" for p in bear) + "<br/><br/>"
    # 退出逻辑
    content += f"<b>退出逻辑</b><br/>&nbsp;&nbsp;类型:{et}<br/>&nbsp;&nbsp;理由:{reason.replace(chr(10), '<br/>&nbsp;&nbsp;')}<br/><br/>"
    content += f"<b>风控参数</b><br/>&nbsp;&nbsp;触发价:{_val(trigger)}<br/>"
    if stop_loss>0: content += f"&nbsp;&nbsp;止损:{_val(stop_loss)}<br/>"
    if is_urgent: content += "<br/><b>请立即执行卖出!</b>"
    elif et == "冲高止盈": content += "<br/><b>建议分批止盈</b>"
    return title, content

def render_exit_signals_batch(signals):
    if not signals: return "", ""
    urgent = any(s.get("exit_type")=="破位止损" or s.get("urgency")=="紧急" for s in signals)
    title = f"{'紧急卖出' if urgent else '卖出信号'} {len(signals)}条"
    content = f"<b>{'紧急卖出!' if urgent else '卖出信号'}</b><br/><br/>"
    for i, s in enumerate(signals):
        _, card = render_exit_signal(s)
        if i < len(signals)-1: card += "<br/><hr/>"
        content += card
    if urgent: content += "<b>请立即处理紧急卖出!</b>"
    return title, content

def render_t0_signal(data):
    name, code = data.get("stock_name",""), data.get("stock_code","")
    signal_type = data.get("signal_type","")
    direction = data.get("direction","")
    pr = data.get("price_range",[0,0])
    t0_shares = data.get("t0_shares",0)
    stop_loss = data.get("stop_loss_price",0)
    holding = data.get("holding_shares",0)
    cost = data.get("cost",0)
    reason = data.get("trigger_reason","")
    time_slot = data.get("time_slot","")
    de = "G" if direction=="正T" else "R"
    title = f"做T {signal_type} {name}({code})"
    content = f"<b>{de} T信号 {name}({code})</b><br/><br/>"
    content += f"<b>持仓概况</b><br/>&nbsp;&nbsp;持仓:{holding}股 | 成本:{_val(cost)}<br/><br/>"
    content += f"<b>交易计划</b><br/>&nbsp;&nbsp;方向:{direction}<br/>"
    content += f"&nbsp;&nbsp;区间:{_val(pr[0])}-{_val(pr[1])}<br/>"
    content += f"&nbsp;&nbsp;数量:{t0_shares}股<br/>"
    if reason: content += f"&nbsp;&nbsp;理由:{reason.replace(chr(10), '<br/>&nbsp;&nbsp;')}<br/>"
    if time_slot: content += f"&nbsp;&nbsp;时段:{time_slot}<br/>"
    content += f"&nbsp;&nbsp;T仓止损:{_val(stop_loss)}<br/><br/>"
    content += f"<b>今日必须了结!</b>"
    return title, content

def render_insight_signal(data):
    st = data.get("type","confirming")
    judgment = data.get("judgment","")
    source = data.get("source","")
    targets = data.get("targets",[])
    track = data.get("track_details","")
    chain = data.get("chain_info","")
    suggestion = data.get("suggestion","")
    if st == "confirming":
        title = "线索兑现中"
        content = f"<b>线索兑现中</b><br/><br/>判断:{judgment}<br/>来源:{source}<br/>跟踪:{track}<br/>{suggestion}<br/>"
        if targets:
            ts = " / ".join(f"{t.get('name','')}({t.get('code','')})" for t in targets)
            content += f"标的:{ts}<br/>"
        if chain: content += f"关联:{chain}<br/>"
    elif st == "refuted":
        title = "逻辑证伪"
        content = f"<b>逻辑证伪</b><br/><br/>判断:{judgment}<br/>来源:{source}<br/>跟踪:{track}<br/>可能已被市场修正<br/>"
    else:
        title = "观点过期"
        content = f"<b>观点过期</b><br/><br/>判断:{judgment}<br/>来源:{source}<br/>已超有效期<br/>"
    return title, content

def render_holding_health(data):
    name, code = data.get("stock_name",""), data.get("stock_code","")
    rating = data.get("rating","")
    rating_detail = data.get("rating_detail","")
    trend = data.get("trend_status","")
    sector = data.get("sector_status","")
    fund = data.get("fund_flow","")
    event = data.get("event_risk","")
    pnl = data.get("pnl_ratio",0)
    adjustment = data.get("mode_adjustment","")
    shares = data.get("shares",0)
    cost = data.get("cost",0)
    price = data.get("current_price",0)
    re = {"健康":"OK","观察":"--","警告":"!!","危险":"!!"}
    title = f"{re.get(rating,'')} {name}({code}) - {rating}"
    content = f"<b>{re.get(rating,'')} {name}({code})</b><br/><br/>"
    rd_str = f" ⓘ {rating_detail}" if rating_detail else ""
    fund_amount = data.get("fund_flow_amount", 0)
    fund_amount_str = _fund_amount(fund_amount)
    fund_display = fund
    if fund_amount_str:
        fund_display += f"({fund_amount_str})"
    sector_name = data.get("sector_name","")
    sn = f"{sector_name}:" if sector_name else ""
    sw2 = data.get("sw_level2","")
    sw3 = data.get("sw_level3","")
    concepts = data.get("concepts","")
    extra = ""
    if sw2: extra += f" | 2级:{sw2}"
    if sw3: extra += f" | 3级:{sw3}"
    if concepts: extra += f" | 概念:{concepts}"
    concept_status = data.get("concept_status","")
    if concept_status: extra += f"({concept_status})"
    content += f"<b>综合评级:{rating}{rd_str}</b><br/>&nbsp;&nbsp;趋势:{trend} | 板块:{sn}{_sector_label(sector)}{extra}<br/>&nbsp;&nbsp;资金:{fund_display} | 事件:{event}<br/>&nbsp;&nbsp;浮盈:{_pct(pnl)}<br/><br/>"
    ma5, ma10, ma20 = data.get("ma5"), data.get("ma10"), data.get("ma20")
    if any(v is not None for v in [ma5,ma10,ma20]):
        content += f"<b>技术细节</b><br/>&nbsp;&nbsp;MA:{_val(ma5)}/{_val(ma10)}/{_val(ma20)}<br/>"
        if price: content += f"&nbsp;&nbsp;现价:{_val(price)}<br/>"
        ts = data.get("tech_signals",{})
        if ts: content += f"&nbsp;&nbsp;三维投票:{'看涨' if ts.get('vote')=='bullish' else '看跌' if ts.get('vote')=='bearish' else '中性'}({ts.get('vote_score',0):+d})<br/>"
        content += "<br/>"
    content += f"<b>收益明细</b><br/>"
    if shares>0 and cost>0: content += f"&nbsp;&nbsp;持仓:{shares}股 x {_val(cost)}<br/>"
    content += f"&nbsp;&nbsp;浮盈:{_pct(pnl)}<br/><br/>"
    content += f"<b>建议</b><br/>&nbsp;&nbsp;{adjustment}<br/>"
    return title, content

def render_environment_overview(data: Dict) -> str:
    """
    渲染环境总览 HTML 片段（可复用于盘前/盘中推送）。

    Args:
        data: {
            "market_mode": str, "market_score": float, "position_limit": float,
            "gem_sci_tech": dict or None, "external_market": dict or None,
            "sectors": {"main_trend":[], "rotational":[], "retreating":[]} or None,
        }
    """
    mode = data.get("market_mode", "defend")
    mn = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(mode, mode)

    content = "<b>📊 环境总览</b><br/>"
    content += f"&nbsp;&nbsp;模式:{mn}<br/>"

    # 大盘环境推导链（真实数据，不虚构评分）
    dimensions = data.get("dimensions") or []
    if dimensions:
        dim_parts = []
        for d in dimensions:
            icon_map = {"bullish": "✅", "neutral": "➖", "bearish": "❌"}
            icon = icon_map.get(d.get("status"), "➖")
            condition = d.get("condition", "")
            dim_parts.append(f"{icon}{d['name']}:{condition}")
        content += f"&nbsp;&nbsp;环境: {' | '.join(dim_parts)}<br/>"
        # 模式判定原因
        mode_reason = data.get("mode_reason", "")
        if mode_reason:
            content += f"&nbsp;&nbsp;判定: {mode_reason} → {mn}<br/>"
        # 外盘降级提示
        if data.get("shock_downgraded"):
            before_cn = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(
                data.get("mode_before_shock", ""), "")
            content += f"&nbsp;&nbsp;外盘降级: {before_cn}→{mn}<br/>"

    # 双创技术位
    gem_sci_tech = data.get("gem_sci_tech") or {}
    if gem_sci_tech:
        trend = gem_sci_tech.get("trend_judgment", "")
        risk = gem_sci_tech.get("risk_flag", "")
        gem = gem_sci_tech.get("gem") or {}
        star = gem_sci_tech.get("star") or {}
        risk_icon = {"danger": "🔴", "warning": "🟡", "none": "🟢"}.get(risk, "")
        content += f"&nbsp;&nbsp;双创:{risk_icon} {trend}"
        gem_label = gem.get("label", "") if gem else ""
        star_label = star.get("label", "") if star else ""
        if gem_label:
            content += f"<br/>&nbsp;&nbsp;&nbsp;&nbsp;创业板: {gem_label}"
        if star_label:
            content += f"<br/>&nbsp;&nbsp;&nbsp;&nbsp;科创50: {star_label}"
        content += "<br/>"

    # 外围市场扰动
    ext_market = data.get("external_market") or {}
    if ext_market:
        disturbance = ext_market.get("disturbance") or {}
        dist_summary = disturbance.get("summary", "")
        dist_level = disturbance.get("level", "")
        if dist_summary:
            level_icon = {"严重扰动": "🔴", "中度扰动": "🟡", "轻度扰动": "🟢", "无影响": "🟢"}.get(dist_level, "")
            content += f"&nbsp;&nbsp;外围:{level_icon} {dist_summary}<br/>"

    # 风格轮动
    style_spread = data.get("style_spread") or {}
    if style_spread:
        style = style_spread.get("style", "")
        spread = style_spread.get("spread", 0)
        if style:
            content += f"&nbsp;&nbsp;风格:{style}(差值{spread:+.1f}%)<br/>"

    # 板块态势
    sectors = data.get("sectors") or {}
    if sectors:
        mt = sectors.get("main_trend", [])
        rs = sectors.get("rotational", [])
        rt = sectors.get("retreating", [])
        if mt or rs or rt:
            content += "<br/><b>板块态势</b><br/>"
            if mt:
                content += f"&nbsp;&nbsp;主线:{','.join(mt)}<br/>"
            if rs:
                content += f"&nbsp;&nbsp;支线:{','.join(rs)}<br/>"
            if rt:
                content += f"&nbsp;&nbsp;退潮:{','.join(rt)}<br/>"

    content += "<br/>"
    return content


def render_pre_market_summary(data):
    mode = data.get("market_mode","defend")
    score = data.get("market_score",5.0)
    pl = data.get("position_limit",0.5)
    text = data.get("pre_market_summary","")
    mn = {"attack":"进攻","defend":"防守","retreat":"撤退"}.get(mode,mode)
    title = f"盘前计划 {mn} {score:.1f}分"
    content = f"<b>盘前计划</b><br/><br/>"
    content += f"<b>环境总览</b><br/>&nbsp;&nbsp;模式:{mn}<br/>"

    # 双创技术位
    gem_sci_tech = data.get("gem_sci_tech") or {}
    if gem_sci_tech:
        trend = gem_sci_tech.get("trend_judgment", "")
        risk = gem_sci_tech.get("risk_flag", "")
        gem = gem_sci_tech.get("gem") or {}
        star = gem_sci_tech.get("star") or {}
        gem_label = gem.get("label", "") if gem else ""
        star_label = star.get("label", "") if star else ""
        risk_icon = {"danger": "🔴", "warning": "🟡", "none": "🟢"}.get(risk, "")
        content += f"&nbsp;&nbsp;双创:{risk_icon} {trend}"
        if gem_label:
            content += f"<br/>&nbsp;&nbsp;&nbsp;&nbsp;创业板: {gem_label}"
        if star_label:
            content += f"<br/>&nbsp;&nbsp;&nbsp;&nbsp;科创50: {star_label}"
        content += "<br/>"

    # 外围市场扰动
    ext_market = data.get("external_market") or {}
    if ext_market:
        disturbance = ext_market.get("disturbance") or {}
        dist_summary = disturbance.get("summary", "")
        dist_level = disturbance.get("level", "")
        if dist_summary:
            level_icon = {"严重扰动": "🔴", "中度扰动": "🟡", "轻度扰动": "🟢", "无影响": "🟢"}.get(dist_level, "")
            content += f"&nbsp;&nbsp;外围:{level_icon} {dist_summary}<br/>"
    bd = data.get("score_breakdown",{})
    if bd:
        content += "<br/><b>评分构成</b><br/>"
        for k,v in bd.items():
            vs = f"{v:.1f}" if isinstance(v,(int,float)) else str(v)
            content += f"&nbsp;&nbsp;{k}:{vs}<br/>"
    sectors = data.get("sectors",{})
    if sectors:
        content += "<br/><b>板块态势</b><br/>"
        mt = sectors.get("main_trend",[])
        rs = sectors.get("rotational",[])
        rt = sectors.get("retreating",[])
        if mt: content += f"&nbsp;&nbsp;主线:{','.join(mt)}<br/>"
        if rs: content += f"&nbsp;&nbsp;支线:{','.join(rs)}<br/>"
        if rt: content += f"&nbsp;&nbsp;退潮:{','.join(rt)}<br/>"
    holdings = data.get("holdings",[])
    if holdings:
        content += "<br/><b>持仓体检</b><br/>"
        for h in holdings:
            pnl_s = _pct(h.get("pnl",0))
            # 有卖出信号的标注
            tag = " 🚨" if h.get("has_exit_signal") else ""
            hd = h.get("health_detail","")
            detail = f" ({hd})" if hd else ""
            content += f"&nbsp;&nbsp;{h.get('name','')}({h.get('code','')}) {h.get('health','')}{detail}{tag}<br/>"
            fund_label = h.get('fund','')
            fund_amt_str = _fund_amount(h.get('fund_amount', 0))
            if fund_amt_str:
                fund_label += f"({fund_amt_str})"
            content += f"&nbsp;&nbsp;&nbsp;&nbsp;趋势:{h.get('trend','')} | 板块:{h.get('sector','')} | 资金:{fund_label} | {pnl_s}<br/>"
            if h.get("mode_adjustment"):
                content += f"&nbsp;&nbsp;&nbsp;&nbsp;→ {h.get('mode_adjustment','')}<br/>"
    entries = data.get("entry_signals",[])
    if entries:
        content += "<br/><b>买入信号</b><br/>"
        for e in entries:
            content += f"&nbsp;&nbsp;📥 {e.get('stock_name','')}({e.get('stock_code','')}) {e.get('entry_type','')} 触发价{_val(e.get('trigger_price',0))}<br/>"
    exits = data.get("exit_signals",[])
    if exits:
        content += "<br/><b>卖出信号</b><br/>"
        for e in exits:
            content += f"&nbsp;&nbsp;🚨 {e.get('stock_name','')}({e.get('stock_code','')}) {e.get('exit_type','')} {e.get('reason','')[:40]}<br/>"
    # 加仓信号（按类型分图标）
    add_signals = data.get("position_build_signals", [])
    if add_signals:
        plan_signals = [a for a in add_signals if a.get("signal_type") == "plan_add"]
        arb_signals = [a for a in add_signals if a.get("signal_type") == "arbitrage_add"]
        focus_signals = [a for a in add_signals if a.get("signal_type") == "focus_add"]

        content += "<br/><b>加仓信号</b><br/>"
        for a in plan_signals:
            level_label = {1: "第一", 2: "第二", 3: "第三"}.get(a.get("add_level", 1), "")
            content += f"&nbsp;&nbsp;📋 {a.get('stock_name','')}({a.get('stock_code','')}) 啄米{level_label}加仓位触发 触发价{_val(a.get('trigger_price',0))}<br/>"
            if a.get("trigger_reason"):
                content += f"&nbsp;&nbsp;&nbsp;&nbsp;{a.get('trigger_reason','')}<br/>"
        for a in arb_signals:
            level_label = {1: "首次", 2: "二次", 3: "三次"}.get(a.get("add_level", 1), "")
            content += f"&nbsp;&nbsp;➕ {a.get('stock_name','')}({a.get('stock_code','')}) 套利{level_label}加仓 触发价{_val(a.get('trigger_price',0))}<br/>"
            if a.get("trigger_reason"):
                content += f"&nbsp;&nbsp;&nbsp;&nbsp;触发: {a.get('trigger_reason','')}<br/>"
        for a in focus_signals:
            content += f"&nbsp;&nbsp;🎯 {a.get('stock_name','')}({a.get('stock_code','')}) 聚焦加仓 触发价{_val(a.get('trigger_price',0))}<br/>"
            if a.get("trigger_reason"):
                content += f"&nbsp;&nbsp;&nbsp;&nbsp;{a.get('trigger_reason','')}<br/>"
    watch = data.get("watchlist_analyses",[])
    if watch:
        # 分类展示：有信号的 vs 无信号的
        sig_stocks = [w for w in watch if w.get("has_signal")]
        nosig_stocks = [w for w in watch if not w.get("has_signal")]
        if sig_stocks:
            content += "<br/><b>自选·有信号</b><br/>"
            for w in sig_stocks:
                entry_line = f"&nbsp;&nbsp;📡 {w.get('name','')}({w.get('code','')}) {w.get('reason','')}"
                # 如有综合体检数据，附上健康评级
                hl = w.get("health")
                if hl:
                    entry_line += f" [{hl}]"
                content += entry_line + "<br/>"
        if nosig_stocks:
            content += "<br/><b>自选·无信号</b><br/>"
            for w in nosig_stocks:
                # 过滤状态 + 综合体检（与持仓同款分析）
                fp = w.get("filter_passed")
                ff = w.get("filter_failed", [])
                reason = w.get("reason","")
                hl = w.get("health")          # 健康评级
                hd = w.get("health_detail")   # 评级明细
                trend = w.get("trend","")     # 趋势
                sector = w.get("sector","")   # 板块
                fund = w.get("fund","")       # 资金
                fund_amt = w.get("fund_amount", 0)

                # 前置检查结果（8项：均线/MA5/量能/涨幅/ST/事件/财务/减持）
                if fp is False and ff:
                    filter_line = f"❌ {'; '.join(ff)}"
                elif fp is True:
                    filter_line = "✅ 8项检查通过"
                elif fp is None:
                    filter_line = "⚠ 分析未完成"
                else:
                    filter_line = reason or ""
                # 综合体检行
                if hl:
                    hd_str = f" ({hd})" if hd else ""
                    fund_str = _fund_amount(fund_amt)
                    fund_display = fund + (f"({fund_str})" if fund_str else "")
                    content += (f"&nbsp;&nbsp;{w.get('name','')}({w.get('code','')}) "
                                f"{filter_line} | {hl}{hd_str}<br/>"
                                f"&nbsp;&nbsp;&nbsp;&nbsp;趋势:{trend} | 板块:{sector} | 资金:{fund_display} | {reason}<br/>")
                else:
                    # 无体检数据（v3 信号摘要或分析失败）
                    content += f"&nbsp;&nbsp;{w.get('name','')}({w.get('code','')}) {filter_line} | {reason}<br/>"
    return title, content
