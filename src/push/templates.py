"""
消息模板渲染 - 完整决策卡片版
每个信号展示完整决策链：环境->技术->形态->过滤->信号逻辑->风控
"""
from typing import Dict

from ..analyzers.volume_pattern import build_volume_pattern, pattern_summary_line, render_volume_pattern
from ..rules_version import get_rules_version

def _pct(v: float, digits: int = 1) -> str:
    return f"+{v*100:.{digits}f}%" if v >= 0 else f"{v*100:.{digits}f}%"

def _val(v, digits: int = 2) -> str:
    if v is None: return "N/A"
    try: return f"{float(v):.{digits}f}"
    except (TypeError, ValueError): return str(v)

def _esc(s) -> str:
    """HTML 转义自由文本（信号理由/备注/API 详情等可能含原始 < > &）。
    未转义的 < > 会形成畸形 HTML，导致 PushPlus 服务端校验拒绝（code 999，2026-08-31）。"""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stock_identity(data) -> str:
    """独立推送的稳定首行标识：标的名称(代码)。"""
    name = str((data or {}).get("stock_name") or "").strip() or "未知标的"
    code = str((data or {}).get("stock_code") or "").strip() or "未知代码"
    return f"{name}({code})"

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


def _signed_amount(amount) -> str:
    """Format a signed fund-flow amount without forcing a source label."""
    if amount is None:
        return ""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return ""
    direction = "流入" if amt > 0 else ("流出" if amt < 0 else "平衡")
    abs_amt = abs(amt)
    if abs_amt >= 100_000_000:
        return f"{direction}{abs_amt/100_000_000:.2f}亿"
    if abs_amt >= 10_000:
        return f"{direction}{abs_amt/10000:.0f}万"
    return f"{direction}{abs_amt:.0f}元"


def _main_flow_window(fund: dict) -> str:
    """Return a truthful window label for mixed fund-flow sources.

    P1-7：覆盖不足时改标实际覆盖天数（近N日净额），覆盖完整才允许
    显示 expected 日窗口，禁止“3日净额(覆盖1/3日)”式误导组合。
    """
    expected_days = int(fund.get("flow_expected_days") or 0)
    covered_days = int(fund.get("flow_coverage_days") or 0)
    if expected_days > 0:
        partial = "" if covered_days >= expected_days else ",部分覆盖降权"
        if covered_days >= expected_days:
            return f"{expected_days}日窗口(覆盖{covered_days}/{expected_days}日)"
        return f"近{covered_days}日净额(覆盖{covered_days}/{expected_days}日{partial})"
    source = str(fund.get("source") or "")
    count = len(fund.get("main_flows") or [])
    return f"{count}日累计" if count else "窗口:N/A"


def _order_flow_text(tech: dict) -> str:
    flow = tech.get("order_flow") or {}
    if not flow.get("available"):
        return "内外盘:N/A(展示)"

    def hand(value):
        value = float(value or 0)
        return f"{value / 10000:.2f}万手" if abs(value) >= 10000 else f"{value:.0f}手"

    outer = hand(flow.get("outer_volume"))
    inner = hand(flow.get("inner_volume"))
    imbalance = float(flow.get("imbalance_pct") or 0)
    return f"外盘{outer}/内盘{inner}({imbalance:+.1f}%,展示)"


def _sector_label(s: str) -> str:
    """板块状态英文→中文"""
    return {"main_trend": "主线", "rotational": "轮动", "retreating": "退潮",
            "unknown": "未知", "主线": "主线", "支线": "支线", "退潮": "退潮"}.get(s, s or "N/A")


def _execution_plan(data) -> str:
    """Render the single-source execution plan for an entry signal."""
    plan = data.get("execution_plan") or {}
    if not plan:
        return ""
    parts = []
    benchmark = plan.get("benchmark_price")
    rrr = plan.get("rrr_low")
    if benchmark:
        rrr_text = f"RRR1:{rrr:.2f}" if rrr else "RRR:N/A"
        parts.append(f"基准:{_val(benchmark)} | {rrr_text} | 风险:{_pct(plan.get('risk_pct') or 0)}")
    volume = plan.get("volume_snapshot") or {}
    turnover = volume.get("turnover_rate")
    turnover_p90 = volume.get("turnover_p90")
    if turnover is not None:
        p90_text = f"P90={float(turnover_p90):.2f}%" if turnover_p90 is not None else "P90=N/A"
        parts.append(f"换手:{float(turnover):.2f}%({p90_text},{volume.get('label', '数据不足')})")
    fund = plan.get("fund_snapshot") or {}
    if fund.get("main_flows"):
        inflow_days = sum(1 for value in fund["main_flows"] if float(value) > 0)
        strong_text = ",强节奏" if fund.get("main_strong") else ""
        parts.append(
            f"主力{_main_flow_window(fund)}:{_signed_amount(sum(fund['main_flows']))}"
            f"({inflow_days}/{len(fund['main_flows'])}日流入{strong_text})"
        )
    if fund.get("latest_super_large_net") is not None or fund.get("latest_large_net") is not None:
        super_text = _signed_amount(fund.get("latest_super_large_net")) or "N/A"
        large_text = _signed_amount(fund.get("latest_large_net")) or "N/A"
        parts.append(f"订单结构:超大单{super_text}/大单{large_text}({fund.get('order_confirmation', '数据不足')})")
    machine_tags = [str(tag) for tag in (fund.get("machine_tags") or []) if tag]
    if machine_tags:
        parts.append("机器标签:" + "/".join(_esc(tag) for tag in machine_tags))
    details = plan.get("confidence_details") or []
    if details:
        parts.append(
            f"置信:{plan.get('confidence_score', 0)}/{plan.get('applicable_score', 0)}"
            f"({plan.get('confidence', '低')}) {_esc(' | '.join(details))}"
        )
    if float(plan.get("combined_risk_multiplier", 1.0) or 1.0) != 1.0:
        parts.append(f"风险系数:{float(plan['combined_risk_multiplier']):.2f}")
    if float(plan.get("industry_multiplier", 1.0) or 1.0) != 1.0:
        parts.append(f"产业系数:{float(plan['industry_multiplier']):.2f}")
    industry_tags = [str(tag) for tag in (plan.get("industry_tags") or []) if tag]
    if industry_tags:
        parts.append("产业标签:" + "/".join(_esc(tag) for tag in industry_tags))
    for tier in plan.get("execution_tiers") or []:
        name = _esc(str(tier.get("name", "")))
        price = _val(tier.get("price"))
        state = _esc(str(tier.get("state", "")))
        trigger = _esc(str(tier.get("trigger", "")))
        parts.append(f"{name}{price}【{state}】{trigger}")
    notes = plan.get("hard_constraint_notes") or []
    if notes:
        parts.append("约束:" + "/".join(_esc(str(note)) for note in notes))
    return "<br/>&nbsp;&nbsp;".join(parts)


# 【Phase3】估值透镜标签（高增长≠便宜：同增速背后质量天差地别）
_LENS_TAG_LABELS = {
    "valuation_bubble": "估值泡沫",
    "valuation_elevated": "估值偏高",
    "value_growth": "低估真增长",
    "low_base_reversal": "低基数反转",
    "strategic_loss": "战略性亏损(订单加速)",
    "model_loss": "商业模式亏损",
    "analyst_flow_conflict": "资金-研报冲突",
}


def _lens_text(lens) -> str:
    """估值透镜摘要：PE/PB + 分档标签（净利双口径在基本面行主段拼接）。"""
    if not lens or not isinstance(lens, dict):
        return ""
    parts = []
    val = lens.get("valuation") or {}
    pe = _num_or_none(val.get("pe_ttm"))
    pb = _num_or_none(val.get("pb"))
    if pe is not None:
        seg = f"PE(TTM){pe:.1f}倍"
        if pb is not None:
            seg += f"/PB{pb:.1f}"
        source = str(val.get("pe_source") or "")
        if source and source != "TTM":
            seg += f"[{source}]"
        parts.append(seg)
    verdict = lens.get("verdict") or {}
    tags = verdict.get("tags") or []
    hits = [_LENS_TAG_LABELS.get(t, t) for t in tags if t in _LENS_TAG_LABELS]
    if hits:
        parts.append("⚠" + "/".join(hits))
    return " | ".join(parts)


def _fundamental_line(data) -> str:
    """
    【二】基本面行（七问第⑦问）：业绩快报/预告 + 盈利质量 + 财报窗口。
    【Phase3】估值透镜合并展示：净利双口径（增速+绝对值——长光华芯
    +238%但仅 0.30 亿的低基数反转必须现形）+ PE/PB + 估值分档 +
    亏损分型。报告期级数据强制带口径（报告期），防止把过去报告期的
    变化当当下变化。
    """
    fund = data.get("fundamental")
    lens = data.get("valuation_lens") if isinstance(data.get("valuation_lens"), dict) else None
    if not fund or not isinstance(fund, dict):
        # 允许信号侧直接携带 fundamental_note（拒绝/降级摘要）
        note = str(data.get("fundamental_note") or "").strip()
        lens_text = _lens_text(lens)
        return " | ".join(x for x in (note, lens_text) if x)
    parts = []
    profit = _num_or_none(fund.get("profit_yoy"))
    deducted = _num_or_none(fund.get("deducted_yoy"))
    # 【Phase3】净利双口径：增速 + 绝对值（亿元）
    profit_abs = _num_or_none(fund.get("profit_abs"))
    if profit_abs is None and lens:
        profit_abs = _num_or_none((lens.get("verdict") or {}).get("profit_abs"))
    forecast = str(fund.get("forecast_type") or "")
    if profit is not None:
        seg = f"净利{profit:+.1f}%"
        if profit_abs is not None:
            seg += f"({profit_abs:.2f}亿)"
        if deducted is not None:
            seg += f"/扣非{deducted:+.1f}%"
        parts.append(seg)
    elif deducted is not None:
        parts.append(f"扣非{deducted:+.1f}%")
    if forecast:
        seg = f"预告:{forecast}"
        if fund.get("forecast_change_pct") is not None:
            try:
                seg += f"{float(fund['forecast_change_pct'])}%"
            except (TypeError, ValueError):
                pass
        parts.append(seg)
    verdict = (fund.get("verdict") or {}) if isinstance(fund.get("verdict"), dict) else {}
    tags = verdict.get("tags") or []
    if tags:
        label_map = {
            "earnings_bomb": "业绩雷",
            "low_quality_growth": "盈利质量低",
            "report_window": "财报窗口",
        }
        hits = [label_map.get(t, t) for t in tags if t in label_map]
        if hits:
            parts.append("⚠" + "/".join(hits))
    period = str(fund.get("report_period") or "")
    if period:
        parts.append(f"报告期{period[:4]}-{period[4:6] if len(period) >= 6 else ''}")
    # 【Phase3】估值透镜段：PE/PB + 估值分档/低基数反转/亏损分型/冲突标签
    lens_text = _lens_text(lens)
    if lens_text:
        parts.append(lens_text)
    reason_note = str(verdict.get("note") or "")
    if reason_note and not parts:
        parts.append(reason_note)
    return " | ".join(parts)


def _num_or_none(value):
    try:
        v = float(value)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _institutional(data) -> str:
    """
    渲染资金流投票（4 数据源投票 + 具体数值）。

    【Phase3】语义修正：原"机构资金"标签易误导——4 票源全是短周期
    资金流/筹码数据（主力/股东/两融/龙虎榜），不测度研报共识。
    兆易创新（PE 33.9 倍+研报一致买入）曾被标"机构看空(-2票)"。
    现改为双标签：资金投票 + 研报共识（冲突时显式标记，禁止单标签定性）。

    数据来自 tech_data['institutional_holding']，包含：
    - vote_score / vote_label: 资金看多/看空/中性
    - votes: 各数据源详情 (north_bound/lhb/main_force/shareholder)
    - analyst_consensus: 研报共识（valuation_lens 旁路注入，不参与资金票计分）
    """
    inst = data.get("institutional_holding")
    if not inst or not isinstance(inst, dict):
        return ""

    parts = []
    score = inst.get("vote_score", 0)
    label = inst.get("vote_label", "资金中性")
    bull = inst.get("bullish_count", 0)
    bear = inst.get("bearish_count", 0)

    # 总分 + 标签（【Phase3】口径标注：资金流投票，非研报共识）
    emoji = "🟢" if score >= 2 else ("🔴" if score <= -2 else "⚪")
    scope = str(inst.get("label_scope") or "")
    valid_votes = inst.get("valid_vote_sources")
    covered_sources = inst.get("covered_sources")
    if valid_votes is not None:
        parts.append(
            f"{emoji}{label}({score:+d}票,多{bull}/空{bear},"
            f"有效票源{valid_votes}/4"
            + (f",覆盖{covered_sources}/4" if covered_sources is not None else "")
            + ")"
            + (f"[{scope}]" if scope else "")
        )
    else:
        parts.append(
            f"{emoji}{label}({score:+d}票,多{bull}/空{bear})"
            + (f"[{scope}]" if scope else "")
        )

    # 各数据源具体数值
    votes = inst.get("votes", {})
    if votes:
        detail_parts = []
        for src_name, src_data in votes.items():
            if not isinstance(src_data, dict):
                continue
            # 简化数据源名称
            v = src_data.get("vote", 0)
            detail = src_data.get("detail", "")
            if not detail:
                continue
            if src_name == "main_force":
                raw = src_data.get("raw") or {}
                super_large = raw.get("latest_super_large_net")
                large = raw.get("latest_large_net")
                extras = []
                expected_days = int(raw.get("expected_days") or 0)
                if expected_days > 0:
                    covered_days = int(raw.get("coverage_days") or 0)
                    partial = "" if covered_days >= expected_days else ",部分覆盖降权"
                    # P1-7：覆盖不足时改标实际覆盖天数，覆盖完整才显示 expected 日窗口
                    if covered_days >= expected_days:
                        extras.append(f"{expected_days}日窗口覆盖{covered_days}/{expected_days}日")
                    else:
                        extras.append(
                            f"近{covered_days}日净额覆盖{covered_days}/{expected_days}日{partial}"
                        )
                points = raw.get("fund_flow_5d") or []
                if points:
                    labels = []
                    for point in points:
                        date = str(point.get("date", "")).replace("-", "")
                        label = f"{date[-4:-2]}/{date[-2:]}" if len(date) >= 4 else date
                        labels.append(f"{label}{_signed_amount(point.get('value'))}")
                    extras.append("5日" + ",".join(labels))
                if super_large is not None:
                    extras.append(f"超大单{_signed_amount(super_large)}")
                if large is not None:
                    extras.append(f"大单{_signed_amount(large)}")
                if extras:
                    detail += "；" + "/".join(extras)
            short_name = {
                "north_bound": "两融",
                "lhb": "龙虎榜",
                "main_force": "主力",
                "shareholder": "股东",
            }.get(src_name, src_name)
            # 【C-噪音降权】权重标注：主力/股东票权 0.5（拆单噪音/报告期滞后）
            weights = inst.get("effective_vote_weights") or inst.get("vote_weights") or {}
            w = weights.get(src_name) if isinstance(weights, dict) else None
            weight_note = f"（权重{w:g}）" if w is not None and float(w) < 1.0 else ""
            freshness = (inst.get("vote_freshness") or {}).get(src_name) or {}
            fresh_date = str(freshness.get("date") or "")
            if fresh_date:
                weight_note += f"@{fresh_date[-5:].replace('-', '/')}"
            if freshness.get("display_only"):
                weight_note += "(滞后,仅展示)"
            arrow = "↑" if v > 0 else ("↓" if v < 0 else "→")
            detail_parts.append(f"{short_name}{arrow}{detail}{weight_note}")
        if detail_parts:
            parts.append(" | ".join(detail_parts))

    top10 = inst.get("top10_institutional_ratio")
    if isinstance(top10, dict):
        latest = top10.get("latest") or {}
        prev = top10.get("previous") or {}
        latest_ratio = latest.get("ratio")
        if latest_ratio is not None:
            change = top10.get("change_points")
            change_text = f"，变化{change:+.2f}pct" if change is not None else ""
            parts.append(f"前十大机构{latest_ratio:.2f}%{change_text}")

    # 【P2-2】资金拆层：慢变量（主力3日/股东报告期）单独计票，
    # 不参与当日价格投票——时间尺度错配不再污染当日判定。
    layering = inst.get("fund_layering")
    if isinstance(layering, dict) and layering.get("enabled"):
        slow_names = {"main_force": "主力", "shareholder": "股东"}
        slow_bits = []
        for src, vote in (layering.get("sources") or {}).items():
            arrow = "↑" if (vote or 0) > 0 else ("↓" if (vote or 0) < 0 else "→")
            slow_bits.append(f"{slow_names.get(src, src)}{arrow}")
        parts.append(
            f"慢变量计票:{'/'.join(slow_bits)}({layering.get('score', 0):+d}，拆层不投票)"
        )

    # 【Phase3】研报共识旁路（资金是节奏、研报是方向，不混票）
    analyst = inst.get("analyst_consensus")
    if isinstance(analyst, dict) and analyst.get("total"):
        seg = (
            f"研报共识{analyst.get('consensus_label', '无数据')}"
            f"(买入{analyst.get('buy', 0)}/增持{analyst.get('outperform', 0)}"
            f"/中性{analyst.get('neutral', 0)}/减持{analyst.get('reduce', 0)}，"
            f"近90天{analyst.get('total', 0)}份)"
        )
        parts.append(seg)
        if inst.get("flow_analyst_conflict"):
            parts.append("⚠资金与研报反向(双标签呈现，禁止单标签定性)")

    # 【P0-6】大宗交易第五资金源（笔数/折溢价/连续性/营业部）：
    # 近20日折价大宗超阈值强制挂派发确认候选；北交所无两融/龙虎榜时补位。
    block_trade = inst.get("block_trade")
    if isinstance(block_trade, dict) and block_trade.get("as_of"):
        try:
            from ..data_layer.block_trade import (
                block_trade_distribution_trigger,
                render_block_trade_line,
            )
            trigger = block_trade_distribution_trigger(
                block_trade, block_trade.get("float_cap"),
            )
            block_line = render_block_trade_line(block_trade, trigger)
            if block_line:
                parts.append(block_line)
        except Exception:
            pass

    # P1-8：两融/主动差背离裁决行（观察卡 ④资金 同样强制输出，两源矛盾不静默）
    try:
        from ..analyzers.signal_plan import margin_drive_adjudication
        flow = (data.get("tech_signals") or {}).get("order_flow") or {}
        _drive = None
        if flow.get("available") and flow.get("imbalance_pct") is not None:
            _drive = float(flow["imbalance_pct"]) / 100.0
        _conflict, _note = margin_drive_adjudication(inst, _drive)
        if _conflict and _note:
            parts.append(f"⚖{_note}")
    except Exception:
        pass

    return " | ".join(parts)


def _entry_decision_lines(data) -> list[str]:
    """Build the compact six-question entry view from machine-readable fields."""
    plan = data.get("execution_plan") or {}
    tech = data.get("tech_signals") or {}
    lines = []

    trend = ((tech.get("category_votes") or {}).get("trend") or {})
    trend_details = "/".join(trend.get("details") or []) or "无信号"
    lines.append(f"①方向:{trend_details}→{_vote_text(int(trend.get('vote', 0) or 0))}")

    momentum = ((tech.get("category_votes") or {}).get("momentum") or {})
    pattern = ((tech.get("category_votes") or {}).get("pattern") or {})
    rsi = data.get("rsi")
    rsi6 = data.get("rsi6") or tech.get("rsi6")
    rsi_text = f"RSI14={float(rsi):.1f}" if rsi is not None else "RSI14:N/A"
    if rsi6 is not None:
        rsi_text += f" | RSI6={float(rsi6):.1f}"
    bias6 = data.get("bias6")
    if bias6 is None:
        bias6 = tech.get("bias6")
    if bias6 is not None:
        rsi_text += f" | BIAS6={float(bias6):.1f}%"
    rsi_text += "(不投票)"
    pattern_details = "/".join(pattern.get("details") or []) or "无信号"
    lines.append(f"②时机:{rsi_text}+{pattern_details}→{_vote_text(int(pattern.get('vote', 0) or 0))}")

    # 【分型引擎】③量能从陈列升级为分型：涨跌×主动差×量能档×位置档 →
    # 八型之一 + 冲突检测 + 确认条件（原始量比/换手/主动差在"数据"行可复核）
    lines.extend(render_volume_pattern(build_volume_pattern(data)))

    fund = plan.get("fund_snapshot") or {}
    main_flows = fund.get("main_flows") or []
    if main_flows:
        inflow_days = sum(1 for value in main_flows if float(value) > 0)
        strong_text = ",强节奏" if fund.get("main_strong") else ""
        funds_text = (
            f"主力{_main_flow_window(fund)}:{_signed_amount(sum(main_flows))}"
            f"({inflow_days}/{len(main_flows)}日流入{strong_text})"
        )
    else:
        funds_text = "主力资金:N/A"
    if fund.get("latest_super_large_net") is not None or fund.get("latest_large_net") is not None:
        super_text = _signed_amount(fund.get("latest_super_large_net")) or "N/A"
        large_text = _signed_amount(fund.get("latest_large_net")) or "N/A"
        funds_text += f" | 超大单{super_text}/大单{large_text}→{_vote_text(int(fund.get('vote', 0) or 0))}"
    machine_tags = [str(tag) for tag in (fund.get("machine_tags") or []) if tag]
    if machine_tags:
        funds_text += " | " + "/".join(machine_tags)
    lines.append(f"④资金:{funds_text}")

    # P1-8：两融/主动差背离裁决行（拦截栏强制输出，优先级+依据）
    if fund.get("margin_drive_conflict") and fund.get("margin_drive_conflict_note"):
        lines.append(f"⑥裁决:{_esc(fund['margin_drive_conflict_note'])}")

    note = str(data.get("trigger_reason") or data.get("note") or "")
    trigger_text = note.split(" | 调度:", 1)[0] or "无触发明细"
    conflict_note = str(fund.get("fund_tech_conflict_note") or "")
    if conflict_note:
        trigger_text = (
            conflict_note + (" | " + trigger_text if trigger_text != "无触发明细" else "")
        )
    lines.append(f"⑤触发:{_esc(trigger_text)}")
    fundamental_text = _fundamental_line(data)
    if fundamental_text:
        lines.append(f"⑦基本面:{_esc(fundamental_text)}")
    return lines


def _vote_text(vote: int) -> str:
    return {1: "+1", -1: "-1", 0: "中性"}.get(vote, "中性")


def _render_compact_observation_signal(data):
    name = data.get("stock_name", "")
    code = data.get("stock_code", "")
    current_price = data.get("current_price")
    change_pct = data.get("change_pct")
    tech = data.get("tech_signals") or {}
    categories = tech.get("category_votes") or {}
    note = str(data.get("note", "") or "")
    buy_note, _, exit_note = note.partition(" | 卖出: ")

    title = f"观察 {name}({code})"
    content = f"<b>观察 {name}({code})</b>"
    if current_price:
        price_line = f"现价{current_price:.2f}"
        if change_pct is not None:
            try:
                price_line += f" {float(change_pct):+.2f}%"
            except (TypeError, ValueError):
                pass
        content += f" {price_line}"
    content += "<br/><br/>"

    mode = data.get("market_mode", "")
    env_parts = []
    if mode:
        market_text = "进攻" if mode == "attack" else "防守" if mode == "defend" else "撤退"
        market_score = data.get("market_score")
        env_parts.append(
            f"市场:{market_text}"
            + (f"({float(market_score):.1f})" if market_score is not None else "")
        )
    sector_name = data.get("sector_name", "")
    if sector_name:
        env_parts.append(f"板块:{sector_name}({_sector_label(data.get('sector_status', ''))})")
    env_parts.append("闸门:" + {"attack": "全策略可用", "defend": "追强降仓可用(三重门),低吸可用", "retreat": "只减不加"}.get(mode, "常规"))
    content += f"<b>环境</b><br/>&nbsp;&nbsp;{_esc(' | '.join(env_parts))}<br/><br/>"

    trend = categories.get("trend") or {}
    momentum = categories.get("momentum") or {}
    pattern = categories.get("pattern") or {}
    ma5 = data.get("ma5")
    ma10 = data.get("ma10")
    ma20 = data.get("ma20")
    ma_text = ""
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_text = "MA多头排列 | "
        elif ma5 < ma10 < ma20:
            ma_text = "MA空头排列 | "
        else:
            ma_text = "MA交叉震荡 | "
    rsi = data.get("rsi") or tech.get("rsi")
    rsi6 = data.get("rsi6") or tech.get("rsi6")
    rsi_text = f"RSI14={float(rsi):.1f}" if rsi is not None else "RSI14:N/A"
    if rsi6 is not None:
        rsi_text += f" | RSI6={float(rsi6):.1f}"
    bias6 = data.get("bias6")
    if bias6 is None:
        bias6 = tech.get("bias6")
    if bias6 is not None:
        rsi_text += f" | BIAS6={float(bias6):.1f}%"
    rsi_text += "(不投票)"
    content += "<b>七问</b><br/>"
    content += (
        f"&nbsp;&nbsp;①方向:{_esc(ma_text + '/'.join(trend.get('details') or []) or '无信号')}"
        f"→{_vote_text(int(trend.get('vote', 0) or 0))}<br/>"
    )
    content += (
        f"&nbsp;&nbsp;②时机:{_esc(rsi_text)}+"
        f"{_esc('/'.join(pattern.get('details') or []) or '无信号')}"
        f"→{_vote_text(int(pattern.get('vote', 0) or 0))}<br/>"
    )
    # 【分型引擎】③量能从陈列升级为分型（与买入卡同引擎同口径）
    for pattern_line in render_volume_pattern(build_volume_pattern(data)):
        content += f"&nbsp;&nbsp;{_esc(pattern_line)}<br/>"
    content += f"&nbsp;&nbsp;④资金:{_esc(_institutional(data) or '无数据')}<br/>"
    buy_text = _esc(buy_note.replace("买入: ", "", 1) or "无买入拦截明细")
    buy_text = buy_text.replace("\n", "<br/>&nbsp;&nbsp;&nbsp;&nbsp;")
    candidate_reason = str(data.get("candidate_reason") or "")
    if candidate_reason:
        content += f"&nbsp;&nbsp;最近策略:{_esc(candidate_reason)}<br/>"
    exit_text = _esc(exit_note or "无卖出检查明细")
    exit_text = exit_text.replace("\n", "<br/>&nbsp;&nbsp;&nbsp;&nbsp;")
    content += f"&nbsp;&nbsp;⑤拦截:{buy_text}<br/>"
    content += f"&nbsp;&nbsp;⑥风控:{exit_text}<br/>"
    fundamental_text = _fundamental_line(data)
    if fundamental_text:
        content += f"&nbsp;&nbsp;⑦基本面:{_esc(fundamental_text)}<br/>"
    # 【P3-2】第八问驱动源：系统不能只看到价格表象
    driver_text = _driver_line(data)
    if driver_text:
        content += f"&nbsp;&nbsp;⑧驱动源:{_esc(driver_text)}<br/>"
    content += "<br/>"
    return title, content


def _driver_line(data) -> str:
    """【P3-2】第八问驱动源行：业绩/板块/资金/价格驱动（买入卡与观察卡共用）。"""
    driver = None
    if isinstance(data, dict):
        driver = (data.get("hypothesis") or {}).get("driver")
        if not driver:
            driver = ((data.get("execution_plan") or {}).get("hypothesis") or {}).get("driver")
    if not isinstance(driver, dict):
        return ""
    try:
        from ..analyzers.driver_attribution import driver_line
        return driver_line(driver)
    except Exception:
        label = driver.get("dual") or driver.get("label") or ""
        evidence = driver.get("evidence") or ""
        return f"{label}（{evidence}）" if evidence else label


def _render_compact_entry_signal(data):
    name = data.get("stock_name", "")
    code = data.get("stock_code", "")
    entry_type = data.get("entry_type", "")
    plan = data.get("execution_plan") or {}
    current_price = data.get("current_price")
    change_pct = data.get("change_pct")

    title = f"买入 {entry_type} {name}({code})"
    content = f"<b>买入 {entry_type} {name}({code})</b>"
    if current_price:
        price_line = f"现价{current_price:.2f}"
        if change_pct is not None:
            try:
                price_line += f" {float(change_pct):+.2f}%"
            except (TypeError, ValueError):
                pass
        content += f" {price_line}"
    content += "<br/><br/>"

    env_parts = []
    mode = data.get("market_mode", "")
    if mode:
        env_parts.append(f"市场:{'进攻' if mode == 'attack' else '防守' if mode == 'defend' else '撤退'}")
    market_score = data.get("market_score") or plan.get("market_score")
    if market_score is not None:
        env_parts[0] += f"({float(market_score):.1f})"
    sector_name = data.get("sector_name", "") or data.get("sw_level2", "")
    if sector_name:
        env_parts.append(f"板块:{sector_name}({_sector_label(data.get('sector_status', ''))})")
    env_parts.append("闸门:" + {"attack": "全策略可用", "defend": "追强降仓可用(三重门),低吸可用", "retreat": "只减不加"}.get(mode, "常规"))
    content += f"<b>环境</b><br/>&nbsp;&nbsp;{_esc(' | '.join(env_parts))}<br/><br/>"

    content += "<b>决策</b><br/>"
    for line in _entry_decision_lines(data):
        content += f"&nbsp;&nbsp;{_esc(line)}<br/>"
    content += "<br/>"

    # 【一】可证伪假说：因为X，所以在Y买入；若Z认错；若W兑现
    hyp_block = _hypothesis_block(data)
    if hyp_block:
        content += hyp_block

    confidence_details = plan.get("confidence_details") or []
    content += (
        f"<b>置信度</b><br/>&nbsp;&nbsp;"
        f"{plan.get('confidence_score', 0)}/{plan.get('applicable_score', 0)} "
        f"{plan.get('confidence', '低')}"
        f"{_esc('【' + ' | '.join(confidence_details) + '】') if confidence_details else ''}<br/><br/>"
    )

    rrr = plan.get("rrr_low")
    rrr_text = f"RRR1:{float(rrr):.2f}" if rrr else "RRR:N/A"
    content += (
        f"<b>RRR</b><br/>&nbsp;&nbsp;{rrr_text}"
        f"【基准{_val(plan.get('benchmark_price'))},非现价口径】<br/><br/>"
    )

    current = data.get("current_price")
    content += f"<b>分档</b>{f'(现态{_val(current)})' if current else ''}<br/>"
    tiers = plan.get("execution_tiers") or []
    for index, tier in enumerate(tiers):
        prefix = "└" if index == len(tiers) - 1 else "├"
        role_labels = {"main": "主仓位", "probe": "试探仓", "stop": "禁入线"}
        role_label = role_labels.get(str(tier.get("role", "")), "")
        distance = str(tier.get("distance") or tier.get("state", ""))
        conditions = str(tier.get("conditions") or "")
        shares = tier.get("base_shares")
        shares_text = f"→{int(shares):,}股" if shares else ""
        content += (
            f"&nbsp;&nbsp;{prefix}{_esc(str(tier.get('name', '')))} {_val(tier.get('price'))}"
            f"【{_esc(distance)}{('·' + role_label) if role_label else ''}】"
            f"{_esc(str(tier.get('trigger', '')))}"
            f"{('+' + _esc(conditions)) if conditions else ''}{_esc(shares_text)}<br/>"
        )
    content += "<br/>"

    shares = data.get("shares")
    base_shares = plan.get("base_shares")
    if shares and base_shares:
        content += (
            f"<b>仓位链路</b><br/>&nbsp;&nbsp;{int(base_shares):,}股"
            f" × 产业{float(plan.get('industry_multiplier', 1.0)):.2f}"
            f" × 风险{float(plan.get('combined_risk_multiplier', 1.0)):.2f}"
            f" = {int(shares):,}股<br/>"
        )
    elif shares:
        content += f"<b>建议仓位</b><br/>&nbsp;&nbsp;{int(shares):,}股<br/>"

    # 【仓位层→账户层】预算披露透出：调度 note 的预算披露（预算基数/
    # 单笔风险敞口/账户口径/反推隐含账户）此前被 ⑤触发 按" | 调度: "
    # 切掉，买入推送看不到绝对值（蘅东光 9/8 验收：300股×88.56=26,568
    # 敞口→隐含 265 万账户——公式对了，参数是黑箱）
    note_text = str(data.get("note") or "")
    budget_marker = "预算基数"
    if budget_marker in note_text:
        budget_idx = note_text.find(budget_marker)
        start = note_text.rfind(" | ", 0, budget_idx)
        budget_block = note_text[start + 3:] if start >= 0 else note_text
        content += (
            f"<b>预算披露</b><br/>&nbsp;&nbsp;{_esc(budget_block)}<br/><br/>"
        )
    return title, content

def _hypothesis_block(data):
    """【一】可证伪假说渲染：X/Y/Z/W 四要素 + 事件有效期说明"""
    hyp = data.get("hypothesis") or (data.get("execution_plan") or {}).get("hypothesis") or {}
    if not hyp or not hyp.get("sentence"):
        return ""
    lines = []
    x = hyp.get("x", "")
    y = hyp.get("y", 0)
    y_note = hyp.get("y_note", "")
    z = hyp.get("z", 0)
    z_note = hyp.get("z_note", "")
    w = hyp.get("w") or []
    w_note = hyp.get("w_note", "")
    z_ref = hyp.get("z_reference", 0)
    if x:
        lines.append(f"X·因为: {_esc(str(x))[:120]}")
    if y:
        lines.append(f"Y·在{_val(y)}买入({_esc(y_note)})")
    if z:
        ref_text = f"，结构位{_val(z_ref)}" if z_ref else ""
        lines.append(f"Z·若{_esc(z_note)}{_ref_text_adj(ref_text)}出现({_val(z)})→认错离场")
    if w:
        w_text = f"{_val(w[0])}" + (f"~{_val(w[-1])}" if len(w) > 1 and w[-1] > w[0] else "")
        lines.append(f"W·若{_esc(w_note)}出现({w_text})→兑现离场")
    event_id = data.get("event_id") or ""
    if event_id:
        lines.append(_esc(f"事件: {event_id}（N日内回踩买点有效，收盘跌回结构位立即撤单）"))
    if not lines:
        return ""
    return "<b>可证伪假说</b><br/>" + "<br/>".join(f"&nbsp;&nbsp;{l}" for l in lines) + "<br/><br/>"


def _ref_text_adj(ref_text):
    return "(X的直接否定)" if not ref_text else f"(X的直接否定{ref_text})"


def _tech(data):
    parts = []
    ma5, ma10, ma20 = data.get("ma5"), data.get("ma10"), data.get("ma20")
    if ma5 and ma10 and ma20:
        order = "多头排列" if ma5 > ma10 > ma20 else "空头排列" if ma5 < ma10 < ma20 else "交叉震荡"
        parts.append(f"MA:{order}({_val(ma5)}/{_val(ma10)}/{_val(ma20)})")
    volume_snapshot = data.get("tech_signals", {}).get("volume_snapshot", {})
    if not isinstance(volume_snapshot, dict):
        volume_snapshot = {}
    early = bool(volume_snapshot.get("is_early_window"))
    vr = (
        volume_snapshot.get("same_period_volume_ratio")
        if early
        else volume_snapshot.get("volume_ratio_effective", data.get("volume_ratio"))
    )
    if vr:
        if vr < 1.0:
            label = "缩量"
        elif vr > 1.2:
            label = "放量"
        else:
            label = "量平"
        source = (
            volume_snapshot.get("same_period_caliber")
            if early
            else volume_snapshot.get("volume_ratio_caliber")
            or data.get("volume_ratio_source")
            or "口径未标注"
        )
        sample_time = (
            volume_snapshot.get("same_period_sample_time")
            if early
            else volume_snapshot.get("volume_ratio_sample_time")
        )
        time_text = f"@{sample_time}" if sample_time else "@时间未标注"
        volume_text = f"量比:{label}{vr:.2f}x@{sample_time or '时间未标注'}(口径:{source}"
        prev_ratio = volume_snapshot.get("volume_vs_prev_day")
        if prev_ratio is not None:
            volume_text += f",较前日{float(prev_ratio):.2f}x"
        parts.append(volume_text + ")")
    else:
        parts.append("量比:数据未取到(早盘需同期量比)" if early else "量比:数据未取到")
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
                ema_label = f"多头({_val(ema_short)}&gt;{_val(ema_long)})"
            elif ema_short < ema_long:
                ema_label = f"空头({_val(ema_short)}&lt;{_val(ema_long)})"
            else:
                ema_label = "粘合"
        else:
            ema_label = "-"
        parts.append(f"EMA:{ema_label}")
        rsi_val = data.get("rsi") or ts.get("rsi")
        rsi6_val = data.get("rsi6") or ts.get("rsi6")
        bias6_val = data.get("bias6")
        if bias6_val is None:
            bias6_val = ts.get("bias6")
        if rsi_val:
            zone = ts.get("rsi_signal", "")
            rsi_text = f"RSI14:{rsi_val:.1f}"
            if rsi6_val:
                rsi_text += f"/RSI6:{rsi6_val:.1f}"
            if bias6_val is not None:
                rsi_text += f"/BIAS6:{float(bias6_val):.1f}%"
            parts.append(f"{rsi_text}({zone})" if zone else rsi_text)
        kdj = ts.get("kdj", {})
        if kdj:
            parts.append(f"KDJ:{float(kdj.get('k', 0)):.1f}/{float(kdj.get('d', 0)):.1f}/J{float(kdj.get('j', 0)):.1f}")
        obv = ts.get("obv", {})
        if obv.get("direction"):
            parts.append(f"OBV:{obv.get('direction')}")
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
            category_labels = {
                "trend": "①方向", "momentum": "②时机",
                "pattern": "③结构", "volume": "④量能",
            }
            categories = ts.get("category_votes") or {}
            vote_values = []
            for category, label in category_labels.items():
                info = categories.get(category) or {}
                value = _num_or_none(info.get("vote"))
                weight = _num_or_none(info.get("weight"))
                vote_values.append(
                    f"{label}{value:+.1f}" if value is not None else f"{label}数据未取到"
                )
            detail_values = " ".join(vote_values)
            parts.append(f"投票:{vote}({sc:+.1f})")
            parts.append(f"评分明细: {detail_values} ⓘ {_esc(top_details)}")
        else:
            parts.append(f"投票:{vote}({sc:+.1f})")
    turnover = data.get("turnover_rate")
    if turnover:
        parts.append(f"换手:{float(turnover):.2f}%")
    order_text = _order_flow_text(ts)
    if order_text:
        parts.append(order_text)
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
        lines.append("&nbsp;&nbsp;" + ("OK " if p else "X ") + ck + (f" ({_esc(d)})" if d else ""))
    return "<br/>".join(lines)

def render_entry_signal(data):
    name, code = data.get("stock_name",""), data.get("stock_code","")
    et = data.get("entry_type","")
    if data.get("execution_plan"):
        return _render_compact_entry_signal(data)
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

    # 板块显示：名称(状态)
    sector_name = data.get("sector_name","") or data.get("sw_level2","")
    sw2 = data.get("sw_level2","")

    if sector_name:
        # 区分显示：如果 sw_level2 与 sector_name 不同，说明 sector_name 是 Level1，补充显示 Level2
        if sw2 and sw2 != sector_name:
            sector_display = f"{sector_name}→{sw2}({_sector_label(sector_status)})"
        else:
            sector_display = f"{sector_name}({_sector_label(sector_status)})"
    else:
        sector_display = f"{_sector_label(sector_status)}"

    env_parts.append(f"板块:{sector_display}")
    content += f"<b>环境</b><br/>&nbsp;&nbsp;{' | '.join(env_parts)}<br/><br/>"
    content += f"<b>技术面</b><br/>&nbsp;&nbsp;{_tech(data)}<br/><br/>"
    plan_display = _execution_plan(data)
    if plan_display:
        content += f"<b>执行计划</b><br/>&nbsp;&nbsp;{plan_display}<br/><br/>"
    # 机构持仓打分（4 数据源投票 + 具体数值）
    inst_display = _institutional(data)
    if inst_display:
        content += f"<b>资金投票</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
    content += f"<b>K线形态</b><br/>&nbsp;&nbsp;{_kline(data)}<br/><br/>"
    fc = data.get("filter_checks",{})
    if fc:
        total, passed = len(fc), sum(1 for c in fc.values() if c.get("passed",True))
        content += f"<b>前置过滤({passed}/{total})</b><br/>{_filter(data)}<br/><br/>"
    content += f"<b>信号逻辑</b><br/>&nbsp;&nbsp;类型:{et}<br/>"
    ssum = data.get("strategy_summary","")
    if ssum: content += f"&nbsp;&nbsp;策略:{_esc(ssum)}<br/>"
    if note: content += f"&nbsp;&nbsp;理由:{_esc(note).replace(chr(10), '<br/>&nbsp;&nbsp;')}<br/>"
    content += f"&nbsp;&nbsp;置信度:{confidence}<br/>"
    tc = {"冲高止盈":"冲高即走","持有观察":"持有观察","主升持有":"中线持有"}.get(target_type,target_type)
    content += f"&nbsp;&nbsp;策略:{tc}<br/><br/>"
    content += f"<b>风控参数</b><br/>&nbsp;&nbsp;触发价:{_val(trigger)}<br/>"
    slr = data.get("stop_loss_reason","")
    content += f"&nbsp;&nbsp;止损:{_val(stop_loss)}" + (f"({_esc(slr)})" if slr else "") + "<br/>"
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
    is_urgent = et == "破位止损" or urgency == "紧急"
    note = data.get("note","")

    # 观察股（无 exit_type，有 note 字段）— 用"观察"标题，不显示退出逻辑
    is_observation = not et and bool(note)
    if is_observation:
        return _render_compact_observation_signal(data)
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
    extra = ""
    if sw2 and sw2 != sector_name: extra += f" | 2级:{sw2}"
    if sw3 and sw3 != sw2 and sw3 != sector_name: extra += f" | 3级:{sw3}"
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
            content += f"<b>资金投票</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
        content += f"<b>说明</b><br/>&nbsp;&nbsp;{_esc(note)}<br/>"
        return title, content

    # 以下仅卖出信号显示
    content += f"<b>技术面</b><br/>&nbsp;&nbsp;{_tech(data)}<br/><br/>"
    # 机构持仓打分（4 数据源投票 + 具体数值）
    inst_display = _institutional(data)
    if inst_display:
        content += f"<b>资金投票</b><br/>&nbsp;&nbsp;{inst_display}<br/><br/>"
    bear = [p for p in data.get("kline_pattern",[]) if "看跌" in p.get("signal","") or "压力" in p.get("signal","")]
    if bear:
        content += "<b>看跌形态</b><br/>&nbsp;&nbsp;"
        content += " ".join(f"{p['pattern']}({p['confidence']})" for p in bear) + "<br/><br/>"
    # 退出逻辑
    content += f"<b>退出逻辑</b><br/>&nbsp;&nbsp;类型:{et}<br/>&nbsp;&nbsp;理由:{_esc(reason).replace(chr(10), '<br/>&nbsp;&nbsp;')}<br/><br/>"
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
    content += "<b>今日必须了结!</b>"
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
    extra = ""
    if sw2 and sw2 != sector_name: extra += f" | 2级:{sw2}"
    if sw3 and sw3 != sw2 and sw3 != sector_name: extra += f" | 3级:{sw3}"
    content += f"<b>综合评级:{rating}{rd_str}</b><br/>&nbsp;&nbsp;趋势:{trend} | 板块:{sn}{_sector_label(sector)}{extra}<br/>&nbsp;&nbsp;资金:{fund_display} | 事件:{event}<br/>&nbsp;&nbsp;浮盈:{_pct(pnl)}<br/><br/>"
    ma5, ma10, ma20 = data.get("ma5"), data.get("ma10"), data.get("ma20")
    if any(v is not None for v in [ma5,ma10,ma20]):
        content += f"<b>技术细节</b><br/>&nbsp;&nbsp;MA:{_val(ma5)}/{_val(ma10)}/{_val(ma20)}<br/>"
        if price: content += f"&nbsp;&nbsp;现价:{_val(price)}<br/>"
        ts = data.get("tech_signals",{})
        if ts: content += f"&nbsp;&nbsp;三维投票:{'看涨' if ts.get('vote')=='bullish' else '看跌' if ts.get('vote')=='bearish' else '中性'}({ts.get('vote_score',0):+d})<br/>"
        content += "<br/>"
    content += "<b>收益明细</b><br/>"
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
    content += f"&nbsp;&nbsp;规则版本:{get_rules_version()}<br/>"
    if data.get("invalidation_triggered"):
        content += "&nbsp;&nbsp;<b>⚠环境判定作废重估告警</b><br/>"
    content += f"&nbsp;&nbsp;模式:{mn}<br/>"
    mode_matrix = data.get("mode_transition_matrix") or []
    if mode_matrix:
        matrix_parts = []
        for row in mode_matrix:
            mark = "→" if row.get("is_current") else ""
            matrix_parts.append(f"{mark}{row.get('label')}:{row.get('entry')}")
        content += (
            f"&nbsp;&nbsp;模式矩阵: "
            f"{' | '.join(_esc(item) for item in matrix_parts)}<br/>"
        )
    from ..decision.mode_rules import render_market_panic_line
    content += f"&nbsp;&nbsp;{_esc(render_market_panic_line(data.get('market_panic')))}<br/>"

    # P2-13 审计（2026-08-22）：非交易日复盘时标注数据参考日（上一交易日）
    ref_date = data.get("ref_date")
    if ref_date:
        suffix = "（上一交易日）" if data.get("is_backfill") else ""
        content += f"&nbsp;&nbsp;数据:{ref_date}{suffix}<br/>"

    # 大盘环境推导链（真实数据，不虚构评分）
    dimensions = data.get("dimensions") or []
    if dimensions:
        dim_parts = []
        for d in dimensions:
            icon_map = {"bullish": "✅", "neutral": "➖", "bearish": "❌"}
            icon = icon_map.get(d.get("status"), "➖")
            condition = d.get("condition", "")
            dim_parts.append(f"{icon}{d['name']}:{_esc(condition)}")
        content += f"&nbsp;&nbsp;环境: {' | '.join(dim_parts)}<br/>"
        score_detail = data.get("score_detail", "")
        if score_detail:
            content += f"&nbsp;&nbsp;{_esc(score_detail)}<br/>"
        # 模式判定原因
        mode_reason = data.get("mode_reason", "")
        if mode_reason:
            content += f"&nbsp;&nbsp;判定: {_esc(mode_reason)} → {mn}<br/>"
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
            content += f"&nbsp;&nbsp;外围:{level_icon} {_esc(dist_summary)}<br/>"

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

    sector_changes = data.get("sector_changes") or {}
    if sector_changes:
        change_items = []
        for item in sector_changes.get("status_changes") or []:
            change_items.append(
                f"{item.get('sector')}: {item.get('from_label')}→"
                f"{item.get('to_label')}"
            )
        for item in sector_changes.get("mapping_changes") or []:
            change_items.append(
                f"{item.get('stock_name')}({item.get('stock_code')}): "
                f"{item.get('from_label')}→{item.get('to_label')}"
            )
        if change_items:
            content += "<br/><b>板块变更</b><br/>"
            content += (
                f"&nbsp;&nbsp;{' | '.join(_esc(item) for item in change_items)}<br/>"
            )
        elif sector_changes.get("baseline"):
            content += (
                "<br/><b>板块变更</b><br/>"
                "&nbsp;&nbsp;基线已建立，次日输出变更<br/>"
            )

    content += "<br/>"
    return content


def render_pre_market_summary(data):
    # 注：本模板尚未接入推送链路（全工程无调用点）；position_limit /
    # pre_market_summary 字段在接线时应被渲染进 content（当前取了未用，已清理）。
    mode = data.get("market_mode","defend")
    score = data.get("market_score",5.0)
    mn = {"attack":"进攻","defend":"防守","retreat":"撤退"}.get(mode,mode)
    title = f"盘前计划 {mn} {score:.1f}分"
    content = "<b>盘前计划</b><br/><br/>"
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
            content += f"&nbsp;&nbsp;外围:{level_icon} {_esc(dist_summary)}<br/>"
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
