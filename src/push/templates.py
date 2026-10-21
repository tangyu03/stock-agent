"""
消息模板渲染 - 完整决策卡片版
每个信号展示完整决策链：环境->技术->形态->过滤->信号逻辑->风控
"""
from collections import Counter
from typing import Dict, List
from datetime import date

from ..analyzers.volume_pattern import build_volume_pattern, pattern_summary_line, render_volume_pattern, volume_gear
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

def _action_watch_price(data, kind: str) -> str:
    """行动摘要的需盯价格：执行档主档/触发价/止损价，取第一个可得者。"""
    plan = data.get("execution_plan") or {}
    tiers = plan.get("execution_tiers") or []
    for tier in tiers:
        role = str(tier.get("role") or "")
        price = tier.get("price")
        if role in ("main", "probe") and price:
            try:
                return f"{_val(float(price))}({_esc(str(tier.get('name') or role))})"
            except (TypeError, ValueError):
                continue
    trigger = data.get("trigger_price")
    if trigger:
        try:
            return f"{_val(float(trigger))}(触发价)"
        except (TypeError, ValueError):
            pass
    stop = data.get("stop_loss_price") or data.get("stop_loss")
    if stop:
        try:
            return f"{_val(float(stop))}(止损价)"
        except (TypeError, ValueError):
            pass
    return ""

def _action_summary_line(data, kind: str) -> str:
    """单票行动摘要行：名称 触发事件 | 待验证条件 | 需盯价格。数据不足返回空串。"""
    name = str((data or {}).get("stock_name") or "").strip()
    code = str((data or {}).get("stock_code") or "").strip()
    if not name or not code:
        return ""
    kind_text = {"买入": "买入", "卖出": "卖出", "观察": "观察"}.get(kind, kind)
    event = str(data.get("entry_type") or data.get("exit_type") or "").strip()
    event_text = f"{kind_text} {event}" if event else kind_text
    event_id = str(data.get("event_id") or "").strip()
    if not event_id:
        event_id = str(((data.get("execution_plan") or {}).get("event_id")) or "").strip()
    event_id_text = f" #{event_id[-8:]}" if event_id else ""

    # 待验证条件：分型确认条件（confirms），无则用确认目标/缺数据不放假结论
    confirm_text = ""
    try:
        vp = build_volume_pattern(data or {})
        if vp.get("available"):
            confirms = [str(c) for c in (vp.get("confirms") or []) if c]
            if confirms:
                bias = vp.get("bias")
                confirm_prefix = "待验(风控)" if bias == -1 else ("待验(转多)" if bias == 1 else "待验")
                confirm_text = " | ".join(confirms[:2])
    except Exception:
        confirm_text = ""

    price_text = ""
    try:
        current = data.get("current_price")
        if current:
            price_text = f"现价{_val(float(current))}"
    except (TypeError, ValueError):
        pass
    watch = _action_watch_price(data, kind)
    if watch:
        price_text = f"{price_text} 盯:{watch}".strip()

    parts = [f"{_esc(name)}({_esc(code)}){_esc(event_id_text)} {_esc(event_text)}"]
    if confirm_text:
        parts.append(f"{confirm_prefix}:{_esc(confirm_text)}")
    if price_text:
        parts.append(f"{_esc(price_text)}")
    return " | ".join(parts)

def render_action_summary(entries=None, exits=None, observations=None) -> str:
    """页首今日行动摘要：一行一人（触发事件｜待验证条件｜需盯价格）。

    P2-17：报告终点原为"防守"二字，读者不知道今天该做什么。行动行从
    分型确认条件（confirms）与执行档/触发/止损价格派生，首屏即可回答
    "今天动不动"。无任何行动时返回空串。
    """
    rows = []
    for s in (entries or []):
        row = _action_summary_line(s, "买入")
        if row:
            rows.append(row)
    for s in (exits or []):
        row = _action_summary_line(s, "卖出")
        if row:
            rows.append(row)
    for s in (observations or []):
        row = _action_summary_line(s, "观察")
        if row:
            rows.append(row)
    if not rows:
        return ""
    block = ["<b>📌 今日行动摘要</b>"]
    block.extend(f"&nbsp;&nbsp;{row}" for row in rows)
    block.append("")
    return "<br/>".join(block)

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


def _fundamental_line(data) -> str:
    """Render only the future calendar; the heavy fundamental gate is removed."""
    calendar_text = _event_calendar_text(data)
    return calendar_text


def _event_calendar_text(data) -> str:
    """未来事件行：财报/减持/解禁等验证事件与剩余天数。"""
    events = data.get("event_calendar") or []
    if not events:
        return ""
    bits = []
    try:
        today = date.today()
    except Exception:
        today = None
    for event in events[:3]:
        event_date = str(event.get("date") or "")[:10]
        days_text = ""
        if today and event_date:
            try:
                days = (date.fromisoformat(event_date) - today).days
                if days >= 0:
                    days_text = f",剩{days}天"
            except ValueError:
                pass
        bits.append(
            f"{event_date} {event.get('type')}"
            f"({event.get('warning_level', '低')}{days_text})"
        )
    return "未来事件:" + " | ".join(bits)


def _num_or_none(value):
    try:
        v = float(value)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _institutional(data) -> str:
    """
    渲染资金流投票（4 数据源投票 + 具体数值）。

    数据来自 tech_data['institutional_holding']，包含：
    - vote_score / vote_label: 机构看多/看空/中性
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
        elif top10.get("available") is False or latest_ratio is None:
            status = str(top10.get("status") or "unavailable")
            status_text = {
                "latest_query_failed_previous_empty": "最新期接口异常,上期空",
                "query_failed": "接口异常",
                "empty_both_periods": "两期均空",
                "latest_empty_previous_ok": "最新期空,展示上期",
                "latest_schema_changed_previous_ok": "最新期字段变动,展示上期",
                "latest_query_failed_previous_ok": "最新期接口异常,展示上期",
                "latest_unavailable_previous_ok": "最新期不可用,展示上期",
                "latest_unavailable_previous_not_ok": "最新期不可用,上期也不可用",
            }.get(status, status)
            if (
                status in (
                    "latest_empty_previous_ok",
                    "latest_schema_changed_previous_ok",
                    "latest_query_failed_previous_ok",
                    "latest_unavailable_previous_ok",
                )
                and prev.get("ratio") is not None
            ):
                prev_text = f"{float(prev['ratio']):.2f}%@{prev.get('date') or '日期未标注'}"
                parts.append(f"前十大机构{status_text},上期{prev_text}")
            else:
                parts.append(f"前十大机构数据不可用({status_text})")

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

    # P1-11 源可用性一致性标注：首日恢复/接口不可用/缓存 必须可见，禁止混合态静默。
    source_status = inst.get("source_status") or {}
    status_notes = []
    status_icons = {
        "recovered_first_day": "首日恢复",
        "disabled": "接口不可用",
    }
    src_labels = {
        "north_bound": "两融", "lhb": "龙虎榜",
        "main_force": "主力", "shareholder": "股东",
    }
    for src_name, status in source_status.items():
        if status in status_icons:
            status_notes.append(
                f"{src_labels.get(src_name, src_name)}{status_icons[status]}"
            )
    if inst.get("source_note"):
        status_notes.append(inst["source_note"])
    if status_notes:
        parts.append("源状态:" + "/".join(status_notes))

    return " | ".join(parts)


def _entry_decision_lines(data) -> list[str]:
    """Build the compact six-question entry view from machine-readable fields."""
    plan = data.get("execution_plan") or {}
    tech = data.get("tech_signals") or {}
    lines = []

    trend = ((tech.get("category_votes") or {}).get("trend") or {})
    trend_details = "/".join(trend.get("details") or []) or "无信号"
    lines.append(f"①方向:{trend_details}→{_vote_text(int(trend.get('vote', 0) or 0))}")

    lines.append(_timing_line(data))

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


def _rsi_note(value, threshold: float, threshold_label: str, gap_label: str) -> str:
    if value is None:
        return "N/A(条件不可评)"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value}(条件不可评)"
    met = number <= threshold
    if met:
        return f"{number:.1f} 触线({threshold_label})"
    gap = abs(number - threshold)
    return f"{number:.1f}({threshold_label},{gap_label}{gap:.1f})"


def _timing_line(data) -> str:
    """时机行：指标展示与阈值差距，四个离散信号单独计数。"""
    tech = data.get("tech_signals") or {}
    rsi6 = data.get("rsi6")
    if rsi6 is None:
        rsi6 = tech.get("rsi6")
    rsi14 = data.get("rsi")
    if rsi14 is None:
        rsi14 = tech.get("rsi")
    bias6 = data.get("bias6")
    if bias6 is None:
        bias6 = tech.get("bias6")

    parts = [f"RSI6={_rsi_note(rsi6, 25, '超卖线25', '还差')}"]
    parts.append(f"RSI14={_rsi_note(rsi14, 40, '弱于40', '超出')}")
    if bias6 is not None:
        try:
            parts.append(f"BIAS6={float(bias6):.1f}%(乖离佐证,不投票)")
        except (TypeError, ValueError):
            parts.append(f"BIAS6={bias6}(乖离佐证,不投票)")

    volume_result = build_volume_pattern(data)
    volume_pattern = volume_result.get("pattern") if volume_result else None
    signals = {
        "金叉": str(((tech.get("macd") or {}).get("state") or "")).startswith("金叉"),
        "底背驰": str(((tech.get("chan_divergence") or {}).get("type") or "")) == "底背驰",
        "缩量止跌": volume_pattern == "抛压衰竭型",
        "放量下破": (
            data.get("change_pct") is not None
            and float(data.get("change_pct") or 0) < 0
            and volume_gear(volume_result.get("volume_ratio")) in ("温和放量", "明显放量", "剧烈放量")
        ),
    }
    hit_count = sum(1 for hit in signals.values() if hit)
    signal_text = "|".join(signals)
    parts.append(f"离散信号{hit_count}/4({signal_text})")
    return "时机: " + " | ".join(parts)


def _fixed_num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _fixed_vote_text(value) -> str:
    number = _fixed_num(value)
    return f"{number:+g}" if number is not None else "N/A"


def _fixed_price_text(value) -> str:
    number = _fixed_num(value)
    return f"{number:g}" if number is not None else "N/A"


def _fixed_confidence(data) -> str:
    return str(data.get("confidence") or "").strip() or "N/A"


def _fixed_environment_text(data) -> str:
    mode = str(data.get("market_mode") or "")
    mode_text = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(mode)
    parts = []
    if mode_text:
        parts.append(f"市场:{mode_text}")
    sector_name = str(data.get("sector_name") or data.get("sw_level2") or "").strip()
    if sector_name:
        sector_status = _sector_label(data.get("sector_status", ""))
        parts.append(f"板块:{sector_name}:{sector_status}" if sector_status else f"板块:{sector_name}")
    return "｜".join(parts) if parts else "N/A"


def _fixed_tech_vote(data):
    tech = data.get("tech_signals") or {}
    vote_score = tech.get("vote_score")
    if vote_score is None:
        vote_score = tech.get("vote")
    if vote_score is None:
        votes = (tech.get("category_votes") or {}).values()
        numbers = []
        for vote in votes:
            number = _fixed_num(vote.get("vote") if isinstance(vote, dict) else vote)
            if number is not None:
                numbers.append(number)
        vote_score = sum(numbers) if numbers else None
    return _fixed_num(vote_score)


def _fixed_tech_sources(data) -> str:
    categories = ((data.get("tech_signals") or {}).get("category_votes") or {})
    names = {
        "trend": "趋势组",
        "momentum": "动量组",
        "pattern": "形态组",
        "volume": "量能组",
    }
    labels = [label for key, label in names.items() if key in categories]
    return "/".join(labels or ["N/A"])


def _fixed_fund_value(data):
    fund = data.get("institutional_holding") or {}
    score = _fixed_num(fund.get("vote_score"))
    covered = _fixed_num(fund.get("covered_sources"))
    return score, covered


def _fixed_clean_rule_text(text, rule_name="") -> str:
    value = str(text or "").strip()
    value = value.split("\n  推导:", 1)[0]
    value = value.split("；量能:", 1)[0]
    value = value.split("\n触发条件:", 1)[-1] if "\n触发条件:" in value else value
    value = value.split("；投票依据:", 1)[0]
    value = value.split("——【分级OR·止损类】", 1)[0]
    value = value.replace("[medium]", "中级").replace("[strong]", "强")
    value = value.replace("此条件已满足", "看空条件成立")
    value = value.strip(" ；\n")
    if rule_name and value.startswith(rule_name):
        value = value[len(rule_name):].lstrip("：:；; ")
    return value or "N/A"


def _fixed_direction_rows(data) -> List[str]:
    ma5, ma10, ma20 = data.get("ma5"), data.get("ma10"), data.get("ma20")
    try:
        if ma5 is not None and ma10 is not None and ma20 is not None:
            ma_text = "MA多头排列" if ma5 > ma10 > ma20 else ("MA空头排列" if ma5 < ma10 < ma20 else "MA交叉震荡")
        else:
            ma_text = "N/A"
    except (TypeError, ValueError):
        ma_text = "N/A"
    tech = data.get("tech_signals") or {}
    macd_text = str(((tech.get("macd") or {}).get("state")) or "")
    if not macd_text:
        for detail in ((tech.get("category_votes") or {}).get("trend") or {}).get("details") or []:
            if str(detail).startswith("MACD"):
                macd_text = str(detail)
                break
    return [
        f"MA排列={ma_text}",
        f"MACD状态={macd_text or 'N/A'}",
    ]


def _volume_anchor(data) -> str:
    result = build_volume_pattern(data)
    pattern = str(result.get("pattern") or "")
    if "真实抛压" in pattern:
        return "!"
    if "分歧" in pattern or result.get("conflict"):
        return "⚠"
    if "待定" in pattern:
        return "~"
    return ""


def _key_levels(data) -> str:
    """Render nearest support/resistance from existing technical anchors."""
    current = _fixed_num(data.get("current_price"))
    if current is None or current <= 0:
        return "支撑:N/A｜压力:N/A"
    tech = data.get("tech_signals") or {}
    boll = tech.get("bollinger") or {}
    support_candidates = [
        ("前低", data.get("prev_low")),
        ("MA5", data.get("ma5")),
        ("MA10", data.get("ma10")),
        ("MA20", data.get("ma20")),
        ("布林下轨", boll.get("lower")),
    ]
    resistance_candidates = [
        ("前高", data.get("prior_high")),
        ("20日高", data.get("recent_high")),
        ("布林上轨", boll.get("upper")),
    ]

    def nearest(candidates, below=True):
        values = []
        for name, value in candidates:
            number = _fixed_num(value)
            if number is None or number <= 0:
                continue
            if below and number < current:
                values.append((name, number))
            if not below and number > current:
                values.append((name, number))
        if not values:
            return ""
        if below:
            name, value = max(values, key=lambda item: item[1])
        else:
            name, value = min(values, key=lambda item: item[1])
        distance = (float(value) / current - 1.0) * 100.0
        return f"{name} {value:g}({distance:+.1f}%)"

    support = nearest(support_candidates, True)
    if not support:
        value = current * 0.95
        support = f"估算现价×0.95={value:g}(-5.0%)"
    resistance = nearest(resistance_candidates, False)
    if not resistance:
        value = current * 1.05
        resistance = f"估算现价×1.05={value:g}(+5.0%)"
    return f"支撑:{support}｜压力:{resistance}"


def _fixed_buy_rows(data, buy_text=None) -> List[str]:
    text = _fixed_buy_text(data, buy_text)
    conclusion, _, event = text.partition("\n事件状态: ")
    conclusion = conclusion.strip() or "N/A"
    event = event.strip()
    result = build_volume_pattern(data)
    confirms = [str(item) for item in (result.get("confirms") or []) if str(item)]
    confirms = _fixed_next_steps(data) if not confirms else confirms[:3]
    return [conclusion, event or "N/A", " | ".join(confirms) or "N/A"]


def build_report_global_context(signals) -> dict:
    """Derive report-wide facts only when every rendered card has the same value."""
    cards = [signal for signal in (signals or []) if isinstance(signal, dict)]
    total = len(cards)
    if not total:
        return {"count": 0, "items": []}

    def common(getter):
        values = [str(getter(card) or "") for card in cards]
        counts = Counter(value for value in values if value)
        if not counts:
            return None
        value, count = counts.most_common(1)[0]
        threshold = max(2, -(-total * 7 // 10))
        if count < threshold:
            return None
        exceptions = [
            f"{card.get('stock_name') or card.get('stock_code')}"
            for card, item in zip(cards, values) if item != value
        ]
        return {"value": value, "count": count, "exceptions": exceptions}

    direction = common(lambda card: "|".join(_fixed_direction_rows(card)))
    buy = common(lambda card: _fixed_buy_text(card).split("\n事件状态: ", 1)[0])
    rule = common(lambda card: _fixed_clean_rule_text(
        card.get("candidate_reason") or card.get("breadth_upgrade_reason") or card.get("reason") or "",
        card.get("exit_type") or "观察",
    ))
    items = []
    if direction:
        items.append({"label": "方向", **direction})
    if rule:
        items.append({"label": "规则", **rule})
    if buy:
        items.append({"label": "买入检查", **buy})
    return {"count": total, "direction": direction, "funds": {}, "items": items}


def render_report_global_context(context) -> str:
    items = (context or {}).get("items") or []
    count = (context or {}).get("count") or 0
    if not items or count < 2:
        return ""
    rows = []
    for item in items:
        exceptions = item.get("exceptions") or []
        row = f"{item['label']}（{item['count']}/{count}）: {item['value']}"
        if exceptions:
            row += "；例外: " + "、".join(str(value) for value in exceptions)
        rows.append(row)
    return "<b>全池口径</b><br/>" + "".join(
        f"&nbsp;&nbsp;{_esc(row)}<br/>" for row in rows
    )


def _fixed_timing_rows(data) -> List[str]:
    tech = data.get("tech_signals") or {}
    rsi6 = data.get("rsi6", tech.get("rsi6"))
    rsi14 = data.get("rsi", tech.get("rsi"))
    bias6 = data.get("bias6", tech.get("bias6"))
    discrete = {
        "金叉": str(((tech.get("macd") or {}).get("state") or "")).startswith("金叉"),
        "底背驰": str(((tech.get("chan_divergence") or {}).get("type") or "")) == "底背驰",
    }
    volume_result = build_volume_pattern(data)
    discrete["缩量止跌"] = volume_result.get("pattern") == "抛压衰竭型"
    change_pct = _fixed_num(data.get("change_pct"))
    discrete["放量下破"] = bool(
        change_pct is not None
        and change_pct < 0
        and volume_gear(volume_result.get("volume_ratio")) in ("温和放量", "明显放量", "剧烈放量")
    )
    hit_count = sum(1 for hit in discrete.values() if hit)
    try:
        bias_text = f"{float(bias6):.1f}%(乖离佐证,不投票)" if bias6 is not None else "N/A"
    except (TypeError, ValueError):
        bias_text = f"{bias6}(乖离佐证,不投票)"
    return [
        f"RSI6={_rsi_note(rsi6, 25, '超卖线25', '还差')}",
        f"RSI14={_rsi_note(rsi14, 40, '弱于40', '超出')}",
        f"BIAS6={bias_text}",
        f"离散={hit_count}/4({'|'.join(discrete)})",
    ]


def _fixed_volume_gear(value) -> str:
    text = str(value or "")
    if "缩量" in text:
        return "缩量"
    if "平量" in text:
        return "平量"
    if "放量" in text:
        return "放量"
    return "N/A"


def _fixed_drive_text(value) -> str:
    number = _fixed_num(value)
    if number is None:
        return "N/A"
    pct = number * 100
    label = "外盘占优" if pct > 0 else ("内盘明显占优" if pct < -5 else "内盘占优")
    return f"{pct:+.1f}%({label})"


def _fixed_turnover_gear(value) -> str:
    number = _fixed_num(value)
    if number is None:
        return "N/A"
    if number > 5:
        return "活跃"
    if number >= 1:
        return "正常"
    return "低迷"


def _fixed_volume_rows(data) -> List[str]:
    result = build_volume_pattern(data)
    if not result or not result.get("available"):
        return ["量比=N/A", "换手=N/A", "主动买卖差=N/A", "位置=N/A", "类型=盘口数据不足"]
    vr = result.get("effective_volume_ratio")
    if vr is not None:
        sample_time = (
            result.get("same_period_sample_time") if result.get("early_window")
            else result.get("volume_sample_time")
        ) or "时间N/A"
        caliber = (
            result.get("same_period_caliber") if result.get("early_window")
            else result.get("volume_caliber")
        ) or "N/A"
        caliber = str(caliber).replace("接口量比", "量比").replace("(5日分钟均量)", "(5日分钟均量口径)")
        vr_text = f"{float(vr):.2f}@{sample_time}({_fixed_volume_gear(result.get('vgear'))};{caliber})"
    else:
        vr_text = "N/A"
    turnover = result.get("turnover")
    turnover_text = (
        f"{float(turnover):.2f}%({_fixed_turnover_gear(turnover)},基准:{result.get('tgear_base') or '绝对档'})"
        if turnover is not None else "N/A"
    )
    position_detail = result.get("position_detail") or {}
    detail_bits = []
    if position_detail.get("dist_high_pct") is not None:
        detail_bits.append(f"{position_detail.get('dist_high_label', '距前高')}{position_detail['dist_high_pct']:+.1f}%")
    if position_detail.get("ma20_dev_pct") is not None:
        detail_bits.append(f"MA20偏离{position_detail['ma20_dev_pct']:+.1f}%")
    if position_detail.get("gain_20d_pct") is not None:
        detail_bits.append(f"20日涨幅{position_detail['gain_20d_pct']:+.1f}%")
    position_text = result.get("position") or "N/A"
    if detail_bits:
        position_text += "(" + "｜".join(detail_bits) + ")"
    type_text = result.get("pattern") or "盘口数据不足"
    conflict = result.get("conflict")
    if conflict:
        type_text = f"{type_text}（{conflict}）"
    return [
        f"量比={vr_text}",
        f"换手={turnover_text}",
        f"主动买卖差={_fixed_drive_text(result.get('drive'))}",
        f"位置={position_text}",
        f"类型={type_text}",
    ]


def _fixed_next_steps(data) -> List[str]:
    result = build_volume_pattern(data)
    confirms = list(result.get("confirms") or [])[:3]
    if not confirms:
        return ["N/A"]
    fund = data.get("institutional_holding") or {}
    votes = fund.get("votes") or {}
    filtered = []
    for item in confirms:
        text = str(item)
        if "主力" in text and not ((votes.get("main_force") or {}).get("raw") is not None):
            continue
        if ("量比" in text) and result.get("effective_volume_ratio") is None:
            continue
        if ("主动" in text or "外盘" in text) and result.get("drive") is None:
            continue
        if ("换手" in text) and result.get("turnover") is None:
            continue
        filtered.append(text)
    return filtered[:3] or ["N/A"]


def _fixed_fund_rows(data) -> List[str]:
    fund = data.get("institutional_holding") or {}
    votes = fund.get("votes") or {}
    source_names = [
        ("两融", "north_bound"),
        ("主力", "main_force"),
        ("龙虎榜", "lhb"),
        ("股东", "shareholder"),
    ]
    rows = []
    for label, key in source_names:
        source = votes.get(key) or {}
        detail = str(source.get("detail") or "").strip()
        if key == "main_force":
            raw = source.get("raw") or {}
            expected_days = int(raw.get("expected_days") or 0)
            covered_days = int(raw.get("coverage_days") or 0)
            if expected_days > 0:
                partial = "" if covered_days >= expected_days else ",部分覆盖弃权"
                detail += f"；近{covered_days}日净额覆盖{covered_days}/{expected_days}日{partial}"
        detail_text = detail or "N/A"
        rows.append(f"{label}={detail_text}")
    score, covered = _fixed_fund_value(data)
    if score is None:
        value_text = "N/A"
    elif score == 0:
        value_text = "弃权"
    else:
        value_text = f"票值{score:+g}"
    coverage_text = f"覆盖{covered if covered is not None else 'N/A'}/4"
    rows.append(f"{coverage_text}｜{value_text}")
    return rows


def _fixed_buy_text(data, buy_text=None) -> str:
    if buy_text is not None:
        return str(buy_text)
    note = str(data.get("note") or "")
    value = note.split(" | 卖出: ", 1)[0].replace("买入: ", "", 1).strip()
    return value or "N/A"


def _fixed_exit_text(data, exit_text=None) -> str:
    if exit_text is not None:
        return str(exit_text)
    note = str(data.get("note") or "")
    parts = note.split(" | 卖出: ", 1)
    return parts[1].strip() if len(parts) == 2 else "N/A"


def _fixed_risk_rows(data, action, rule_name, reason_text, trigger_price=None, stop_loss_price=None) -> List[str]:
    current_price = _fixed_num(data.get("current_price"))
    trigger = _fixed_num(trigger_price)
    if action == "卖出":
        tracking = "触发"
        instruction = "卖出"
    else:
        tracking = "未触发" if "未触发" in _fixed_exit_text(data) else "N/A"
        instruction = "观察"
    if trigger is not None and current_price is not None and trigger == current_price:
        trigger_text = f"收盘确认价{_fixed_price_text(trigger)}"
    elif trigger is not None:
        trigger_text = f"触发价{_fixed_price_text(trigger)}"
    else:
        trigger_text = "确认价:N/A"
    stop = _fixed_num(stop_loss_price)
    stop_text = f"止损:{_fixed_price_text(stop)}" if stop is not None else "止损:N/A"
    return [f"{action}｜跟踪={tracking}｜{trigger_text}｜{stop_text}"]


def _render_fixed_card(
    data,
    action,
    rule_name,
    reason_text,
    buy_text=None,
    exit_text=None,
    trigger_price=None,
    stop_loss_price=None,
):
    name = data.get("stock_name", "")
    code = data.get("stock_code", "")
    current_price = data.get("current_price")
    change_pct = data.get("change_pct")
    action_suffix = "" if not rule_name or rule_name in action else rule_name
    anchor = _volume_anchor(data)
    plan = data.get("execution_plan") or {}
    conf = plan.get("confidence_score")
    appl = plan.get("applicable_score")
    composite = ""
    if conf is not None and appl is not None and appl > 0:
        composite = f" {float(conf) / float(appl) * 10:.1f}"
    title = f"{anchor}{action}{action_suffix}{composite} {name}({code})"

    price_text = ""
    if current_price is not None:
        price_text = f"现价{current_price:.2f}"
        if change_pct is not None:
            try:
                price_text += f" {float(change_pct):+.2f}%"
            except (TypeError, ValueError):
                pass
    fund_score, _ = _fixed_fund_value(data)
    fund_text = "弃权" if fund_score == 0 else f"票值{fund_score:+g}" if fund_score is not None else "N/A"
    stop = _fixed_num(stop_loss_price)
    stop_display = _fixed_price_text(stop) if stop is not None else "N/A"
    judgment_output = f"输出={action}｜止损:{stop_display}｜有效期:N/A"

    lines = [
        f"1 判读:输入={_fixed_environment_text(data)}｜技术票{_fixed_vote_text(_fixed_tech_vote(data))}"
        f"（{_fixed_tech_sources(data)}）｜资金{fund_text}"
    ]
    direction_rows = _fixed_direction_rows(data)
    lines.append(f"规则={rule_name}：{_fixed_clean_rule_text(reason_text, rule_name)}")
    lines.append(judgment_output)
    lines.append(f"2 方向:{direction_rows[0]}")
    lines.extend(direction_rows[1:])
    timing_rows = _fixed_timing_rows(data)
    lines.append(f"3 时机:{timing_rows[0]}")
    lines.extend(timing_rows[1:])
    volume_rows = render_volume_pattern(build_volume_pattern(data)) or _fixed_volume_rows(data)
    if volume_rows and volume_rows[0].startswith("③量能 "):
        volume_rows[0] = volume_rows[0][len("③量能 "):]
    lines.append(f"4 量能:{volume_rows[0]}")
    lines.extend(volume_rows[1:])
    fund_rows = _fixed_fund_rows(data)
    lines.append(f"5 资金:{fund_rows[0]}")
    lines.extend(fund_rows[1:])
    buy_rows = _fixed_buy_rows(data, buy_text)
    lines.append(f"6 买入:结论={buy_rows[0]}")
    lines.append(f"├ 事件:{buy_rows[1]}")
    lines.append(f"└ 缺口:{buy_rows[2]}")
    risk_rows = _fixed_risk_rows(data, action, rule_name, reason_text, trigger_price, stop_loss_price)
    lines.append(f"7 风控:{risk_rows[0]}")
    lines.extend(risk_rows[1:])
    lines.append(f"8 关键价位:{_key_levels(data)}")
    fundamental_text = _fundamental_line(data)
    lines.append(f"9 基本面:业绩={fundamental_text or 'N/A'}")
    calendar_text = _event_calendar_text(data)
    if calendar_text:
        lines.append(f"未来事件={calendar_text}")

    content = f"<b>{_esc(title)}</b>"
    if price_text:
        content += f" {_esc(price_text)}"
    content += "<br/>"
    content += "".join(f"&nbsp;&nbsp;{_esc(line)}<br/>" for line in lines)
    content += "<br/>"
    return title, content


def _render_seven_questions(data, buy_text=None, exit_text=None):
    tech = data.get("tech_signals") or {}
    categories = tech.get("category_votes") or {}
    note = str(data.get("note", "") or "")
    if buy_text is None:
        buy_note, _, note_exit = note.partition(" | 卖出: ")
        buy_text = buy_note.replace("买入: ", "", 1) or "无买入拦截明细"
    if exit_text is None:
        _, _, note_exit = note.partition(" | 卖出: ")
        exit_text = note_exit or "无卖出检查明细"

    content = ""
    trend = categories.get("trend") or {}
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
    content += "<b>七问</b><br/>"
    content += (
        f"&nbsp;&nbsp;①方向:{_esc(ma_text + '/'.join(trend.get('details') or []) or '无信号')}"
        f"→{_vote_text(int(trend.get('vote', 0) or 0))}<br/>"
    )
    content += f"&nbsp;&nbsp;{_esc(_timing_line(data))}<br/>"
    # 【分型引擎】③量能从陈列升级为分型（与买入卡同引擎同口径）
    for pattern_line in render_volume_pattern(build_volume_pattern(data)):
        content += f"&nbsp;&nbsp;{_esc(pattern_line)}<br/>"
    content += f"&nbsp;&nbsp;④资金:{_esc(_institutional(data) or '无数据')}<br/>"
    buy_text = buy_text.replace("\n", "<br/>&nbsp;&nbsp;&nbsp;&nbsp;")
    exit_text = exit_text.replace("\n", "<br/>&nbsp;&nbsp;&nbsp;&nbsp;")
    if buy_text and buy_text != "无买入拦截明细":
        content += f"&nbsp;&nbsp;⑤买入检查:{buy_text}<br/>"
    if exit_text and exit_text != "无卖出检查明细":
        content += f"&nbsp;&nbsp;⑥风控:{exit_text}<br/>"
    fundamental_text = _fundamental_line(data)
    if fundamental_text:
        content += f"&nbsp;&nbsp;⑦基本面:{_esc(fundamental_text)}<br/>"
    # 【P3-2】第八问驱动源：系统不能只看到价格表象
    driver_text = _driver_line(data)
    if driver_text:
        content += f"&nbsp;&nbsp;⑧驱动源:{_esc(driver_text)}<br/>"
    content += "<br/>"
    return content


def _render_compact_observation_signal(data):
    reason = str(data.get("candidate_reason") or data.get("breadth_upgrade_reason") or "")
    return _render_fixed_card(
        data,
        action="观察",
        rule_name="观察",
        reason_text=reason,
        trigger_price=data.get("trigger_price"),
        stop_loss_price=data.get("stop_loss_price"),
    )


def _driver_line(data) -> str:
    """【P3-2】第八问驱动源行：业绩/板块/资金/价格驱动（买入卡与观察卡共用）。"""
    driver = None
    if isinstance(data, dict):
        driver = data.get("driver")
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
    env_parts.append("闸门:" + {"attack": "全策略可用", "defend": "低吸可用", "retreat": "只减不加"}.get(mode, "常规"))
    content += f"<b>环境</b><br/>&nbsp;&nbsp;{_esc(' | '.join(env_parts))}<br/><br/>"

    content += "<b>决策</b><br/>"
    for line in _entry_decision_lines(data):
        content += f"&nbsp;&nbsp;{_esc(line)}<br/>"
    content += "<br/>"

    content += (
        "<b>关键价位</b><br/>&nbsp;&nbsp;"
        f"{_esc(_key_levels(data))}<br/><br/>"
    )

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
    return _render_fixed_card(
        data,
        action="卖出",
        rule_name=et,
        reason_text=reason,
        buy_text="不适用",
        exit_text=reason,
        trigger_price=trigger,
        stop_loss_price=stop_loss,
    )

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
        if ts: content += f"&nbsp;&nbsp;三维投票:{'看涨' if ts.get('vote')=='bullish' else '看跌' if ts.get('vote')=='bearish' else '中性'}({float(ts.get('vote_score', 0) or 0):+.1f})<br/>"
        content += "<br/>"
    content += "<b>收益明细</b><br/>"
    if shares>0 and cost>0: content += f"&nbsp;&nbsp;持仓:{shares}股 x {_val(cost)}<br/>"
    content += f"&nbsp;&nbsp;浮盈:{_pct(pnl)}<br/><br/>"
    content += f"<b>建议</b><br/>&nbsp;&nbsp;{adjustment}<br/>"
    return title, content



def _mode_gate_expectation(mode: str) -> str:
    """环境栏闸门预期提示：模式判定用昨日收盘，今日若收盘站回阈值则明日转档。"""
    if mode == "attack":
        return ""
    if mode == "defend":
        return "今日若收盘站回MA5且多头排列(MA5>MA10>MA20)且派发日≤5，明日转进攻"
    if mode == "retreat":
        return "今日若收盘收复MA5×0.99，明日转防守"
    return ""
def _env_dimension_group(dimension: Dict) -> str:
    key = str(dimension.get("key") or dimension.get("name") or "").lower()
    name = str(dimension.get("name") or "")
    if key in {"index_trend", "ma_alignment"} or "趋势" in name or "均线" in name:
        return "趋势"
    if key == "volume" or "量价" in name:
        return "量价"
    if key == "breadth" or "宽度" in name:
        return "宽度"
    if key == "dist_days" or "派发" in name:
        return "派发"
    return "其他"


def render_environment_overview(data: Dict) -> str:
    """渲染环境总览：模式结论 → 证据 → 辅助 → 板块影响。"""
    mode = data.get("market_mode", "defend")
    mn = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(mode, mode)
    score = data.get("market_score")
    ref_date = data.get("ref_date")
    date_suffix = "（上一交易日）" if data.get("is_backfill") else ""

    header_parts = [f"模式:{mn}"]
    if score is not None:
        try:
            header_parts.append(f"{float(score):.1f}分")
        except (TypeError, ValueError):
            pass
    header_parts.append(f"口径:{get_rules_version()}")
    if ref_date:
        header_parts.append(f"数据:{ref_date}{date_suffix}")

    content = "<b>📊 环境</b><br/>"
    if data.get("invalidation_triggered"):
        content += "&nbsp;&nbsp;<b>⚠环境判定作废重估告警</b><br/>"
    content += f"&nbsp;&nbsp;{'｜'.join(_esc(item) for item in header_parts)}<br/>"

    reason = str(data.get("mode_reason") or "").strip()
    if data.get("shock_downgraded"):
        before_cn = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(
            data.get("mode_before_shock", ""), ""
        )
        downgrade = f"外盘降级:{before_cn}→{mn}" if before_cn else f"外盘降级→{mn}"
        reason = "；".join(item for item in (reason, downgrade) if item)
    if reason:
        content += f"&nbsp;&nbsp;原因:{_esc(reason)}<br/>"

    gate_hint = _mode_gate_expectation(mode)
    if gate_hint:
        content += f"&nbsp;&nbsp;转档:{_esc(gate_hint)}<br/>"

    point_overlay = data.get("point_overlay")
    if isinstance(point_overlay, dict) and point_overlay.get("available"):
        overlay_text = (
            f"行数{point_overlay.get('rows', 0)}｜"
            f"一致{point_overlay.get('matched', 0)}｜"
            f"冲突{point_overlay.get('conflicts', 0)}｜"
            f"参考{point_overlay.get('references', 0)}｜"
            f"过期{point_overlay.get('stale', 0)}"
        )
        content += f"&nbsp;&nbsp;点表覆盖:{_esc(overlay_text)}<br/>"

    panic = data.get("market_panic")
    if isinstance(panic, dict):
        panic_label = "已触发" if panic.get("triggered") else "未触发"
        reasons = "；".join(str(item) for item in panic.get("reasons") or [])
        missing = "；".join(str(item) for item in panic.get("missing") or [])
        panic_detail = reasons or (f"数据未取到:{missing}" if missing else "均未达阈值")
        content += f"&nbsp;&nbsp;恐慌抄底:{_esc(panic_label)}({_esc(panic_detail)})<br/>"
    else:
        content += "&nbsp;&nbsp;恐慌抄底:数据未取到<br/>"

    # 证据层：同类维度合并，过程字段不再平铺。
    dimension_groups: Dict[str, List[str]] = {}
    for dimension in data.get("dimensions") or []:
        group = _env_dimension_group(dimension)
        condition = str(dimension.get("condition") or "").strip()
        if condition:
            dimension_groups.setdefault(group, []).append(condition)
    evidence_lines = []
    for group, values in dimension_groups.items():
        evidence_lines.append(f"{group}:{_esc(' | '.join(values))}")
    score_detail = str(data.get("score_detail") or "").strip()
    if score_detail:
        evidence_lines.append(f"评分:{_esc(score_detail)}")

    # 双创是趋势证据的补充，不是模式结论本体。
    gem_sci_tech = data.get("gem_sci_tech") or {}
    if gem_sci_tech:
        trend = gem_sci_tech.get("trend_judgment", "")
        risk = gem_sci_tech.get("risk_flag", "")
        risk_icon = {"danger": "🔴", "warning": "🟡", "none": "🟢"}.get(risk, "")
        gem = gem_sci_tech.get("gem") or {}
        star = gem_sci_tech.get("star") or {}
        gem_label = gem.get("label", "") if gem else ""
        star_label = star.get("label", "") if star else ""
        gem_detail = "；".join(item for item in (
            trend,
            f"创业板 {gem_label}" if gem_label else "",
            f"科创50 {star_label}" if star_label else "",
        ) if item)
        evidence_lines.append(f"双创:{risk_icon} {_esc(gem_detail)}")
    if evidence_lines:
        content += "<b>证据</b><br/>"
        for item in evidence_lines:
            content += f"&nbsp;&nbsp;{item}<br/>"

    # 辅助层：只观察、不投票。
    auxiliary = []
    ext_market = data.get("external_market") or {}
    if ext_market:
        disturbance = ext_market.get("disturbance") or {}
        summary = disturbance.get("summary", "")
        if summary:
            level_icon = {
                "严重扰动": "🔴", "中度扰动": "🟡",
                "轻度扰动": "🟢", "无影响": "🟢",
            }.get(disturbance.get("level", ""), "")
            auxiliary.append(f"外围{level_icon} {_esc(summary)}")
    style_spread = data.get("style_spread") or {}
    if style_spread.get("style"):
        auxiliary.append(
            f"风格 {_esc(str(style_spread['style']))}（差值{style_spread.get('spread', 0):+.1f}%）"
        )
    resonance = data.get("sector_resonance")
    if isinstance(resonance, dict) and resonance.get("counts"):
        counts = resonance.get("counts") or {}
        total = int(resonance.get("total") or 0)
        if total > 0:
            resonance_parts = [
                f"主线{counts.get('main_trend', 0)}",
                f"支线{counts.get('rotational', 0)}",
                f"退潮{counts.get('retreating', 0)}",
            ]
            if counts.get("unknown"):
                resonance_parts.append(f"未知{counts['unknown']}")
            status_labels = {
                "bullish_resonance": "同向共振",
                "bearish_resonance": "同向弱势",
                "bullish_divergence": "底背离观测",
                "bearish_divergence": "顶背离观测",
                "mixed": "混合观测",
            }
            status = str(resonance.get("status") or "")
            if status in status_labels:
                resonance_parts.append(status_labels[status])
            lag_days = int(resonance.get("lag_days") or 0)
            if lag_days > 0:
                resonance_parts.append(f"快照滞后{lag_days}天，仅观测不投票")
            elif resonance.get("snapshot_date"):
                resonance_parts.append("当日快照，仅观测不投票")
            auxiliary.append("共振 " + "｜".join(_esc(item) for item in resonance_parts))
    elif isinstance(resonance, dict) and resonance.get("line"):
        auxiliary.append("共振 " + _esc(str(resonance["line"])))
    if auxiliary:
        content += "<b>辅助</b><br/>"
        for item in auxiliary:
            content += f"&nbsp;&nbsp;{item}<br/>"

    # 板块影响层：态势看数量，变更看具体迁移；映射影响按同迁移聚合。
    sectors = data.get("sectors") or {}
    sector_risk = data.get("sector_snapshot_risk") or {}
    sector_changes = data.get("sector_changes") or {}
    if sectors or sector_risk or sector_changes:
        content += "<b>板块</b><br/>"
        if sector_risk:
            content += f"&nbsp;&nbsp;风险:{_esc(str(sector_risk.get('line') or '板块快照风险'))}<br/>"
        if sectors:
            mt = sectors.get("main_trend", [])
            rs = sectors.get("rotational", [])
            rt = sectors.get("retreating", [])
            sector_parts = []
            if mt:
                sector_parts.append(f"主线{len(mt)}只")
            if rs:
                sector_parts.append(f"支线{len(rs)}只")
            if rt:
                sector_parts.append(f"退潮{len(rt)}只")
            if sector_parts:
                content += f"&nbsp;&nbsp;态势:{_esc('｜'.join(sector_parts))}<br/>"
        if sector_changes:
            change_items = [
                f"状态:{item.get('sector')} {item.get('from_label')}→{item.get('to_label')}"
                for item in sector_changes.get("status_changes") or []
            ]
            label_groups: Dict[str, List[str]] = {}
            for item in sector_changes.get("mapping_changes") or []:
                from_label = str(item.get("from_label") or "")
                to_label = str(item.get("to_label") or "")
                if from_label == to_label:
                    continue
                key = f"{from_label}→{to_label}"
                name = str(item.get("stock_name") or "")
                code = str(item.get("stock_code") or "")
                label_groups.setdefault(key, []).append(f"{name}({code})")
            for label_change, stocks in label_groups.items():
                change_items.append(f"映射:{'、'.join(stocks)}（{label_change}）")
            if change_items:
                content += f"&nbsp;&nbsp;变更:{_esc('；'.join(change_items))}<br/>"
            elif sector_changes.get("baseline"):
                content += "&nbsp;&nbsp;变更:基线已建立，次日输出变更<br/>"

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
