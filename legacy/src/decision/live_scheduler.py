"""
P3: 实盘信号调度器 — 信号服务模式（不维护持仓）
==============================================
将回测层 Step0 的调度思路应用到实盘，但按"信号服务"模式运行：
框架只负责输出买卖建议信号，不读取/维护真实持仓（持仓回执闭环繁琐，
且本框架的持仓状态不是决策前提——买卖信号对自选池全量扫描产生）。

与回测层 SignalScheduler 的区别：
- 回测层：维护模拟持仓状态，按日推进
- 实盘层：单次调度，纯信号输出——卖出信号无条件全量输出，
  买入信号全量输出，仅按入场类型优先级排序

【三】受众分流（本实盘链路新增）：
  买入事件只对空仓者成立；对持仓者的输出永远是
  持有/加仓/减仓/止损四选一（不再把持仓者当空仓者反复推销）。
  持仓来源：回执闭环聚合（trade_logger.get_current_holdings）。

【一】假说门 + 【六】策略下线：
  - execution_plan.execute=False（假说不完整）→ 不调度不入 buy
  - 策略 offline（滚动50笔期望为负/胜率破线）→ 过滤并统计

核心逻辑：
1. 卖出信号：全部输出（不校验是否持仓——持仓由用户自己管理）
2. 买入按入场类型优先级排序：价量突破 > 套利低吸 > 恐慌抄底 > 确认追强，
   同优先级按 信心→紧急度→股票代码 兜底排序，保证调度可复现（P1-1 审计 2026-08-18）。
3. 不做数量上限、总预算、剩余资金或碎单拦截；资金管理由用户自行处理

使用方式：
    from src.decision.live_scheduler import schedule_live_signals
    scheduled = schedule_live_signals(entry_signals, exit_signals, total_asset)
    # scheduled = {'buy': [...], 'sell': [...], 'position_advice': [...], 'skipped': {...}}
"""
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ============================================================
# 调度参数（与 Step0 / position.yaml 一致）
# ============================================================
def _positive_price(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

# 入场类型优先级（调度排序用，值越大越优先；纯类型偏好，非收益预测）
ENTRY_PRIORITY = {
    '价量突破': 4,
    '套利低吸': 3,
    '恐慌抄底': 2,
    '确认追强': 1,
}


@dataclass
class ScheduledSignal:
    """调度后信号"""
    stock_code: str
    stock_name: str
    action: str  # 'buy' / 'sell' / 'position'（持仓四选一）
    entry_type: str = ''
    exit_type: str = ''
    trigger_price: float = 0.0
    stop_loss_price: float = 0.0
    shares: int = 0
    reason: str = ''
    urgency: str = '常规'
    confidence: str = '中'
    benchmark_price: float = 0.0
    rrr_low: float = 0.0
    risk_multiplier: float = 1.0
    industry_multiplier: float = 1.0
    execution_plan: dict = None
    market_mode: str = 'defend'
    # 调度信息
    schedule_note: str = ''  # 调度原因说明
    action_size: float = 1.0  # 点位独立动作的目标仓位比例
    # 【三】受众：empty=空仓者买入 / holding=持仓者建议
    audience: str = 'empty'
    # 【三】持仓四选一动作：持有/加仓/减仓/止损（action='position' 时有效）
    position_action: str = ''
    # 驱动源（推送给模板/落库用）
    driver: dict = field(default_factory=dict)


def _parse_entry_type(reason: str) -> str:
    """从 reason 解析 entry_type"""
    if not reason:
        return ''
    # reason 格式: "价量突破: 站上MA25..." 或 "MA5压制: ..."
    if ':' in reason:
        return reason.split(':', 1)[0].strip()
    return reason.strip()


def build_point_scheduled_signals(
    point_decisions: Optional[List[Dict]],
    market_mode: str = 'defend',
) -> Dict[str, List[ScheduledSignal]]:
    """Convert touched point rows into scheduled signals."""
    result = {'point_buy': [], 'point_sell': []}
    for item in point_decisions or []:
        action = str(item.get('action') or '').upper()
        if action in {'ADD', 'BUY_FIRST'}:
            action = 'BUY'
        elif action in {'STOP_DELAY', 'SELL_PART', 'CLEAR'}:
            action = 'SELL'
        if action not in {'BUY', 'SELL'}:
            continue
        code = str(item.get('stock_code') or '')
        if not code:
            continue
        zone_low = _positive_price(item.get('zone_low'))
        zone_high = _positive_price(item.get('zone_high'))
        trigger_price = (zone_low + zone_high) / 2
        source = str(item.get('source') or 'POINT')
        action_size = max(0.0, min(1.0, _positive_price(item.get('action_size')) or 1.0))
        reverse_action = str(item.get('reverse_action') or '')
        invalid_if = str(item.get('invalid_if') or '')
        schedule_note = (
            f"独立点位：目标{action_size:.0%} | 反向:{reverse_action} | 失效:{invalid_if}"
        )
        reason = str(item.get('reason') or f"触发{source}点位")
        if action == 'BUY':
            result['point_buy'].append(ScheduledSignal(
                stock_code=code,
                stock_name=str(item.get('stock_name') or code),
                action='buy',
                entry_type=f"POINT:{source}",
                trigger_price=trigger_price,
                stop_loss_price=zone_low,
                shares=0,
                reason=reason,
                market_mode=market_mode,
                schedule_note=schedule_note,
                audience='empty',
                action_size=action_size,
            ))
        else:
            result['point_sell'].append(ScheduledSignal(
                stock_code=code,
                stock_name=str(item.get('stock_name') or code),
                action='sell',
                exit_type=f"POINT:{source}",
                trigger_price=trigger_price,
                reason=f"{reason} | 目标减仓{action_size:.0%}",
                market_mode=market_mode,
                schedule_note=schedule_note,
                audience='holding',
                action_size=action_size,
            ))
    return result


def schedule_live_signals(
    entry_signals: List[Dict],
    exit_signals: List[Dict],
    total_asset: float = 1_000_000,
    market_mode: str = 'defend',
    holdings: Optional[List[Dict]] = None,
    offline_strategies: Optional[List[str]] = None,
    point_decisions: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    实盘信号调度器（信号服务模式 + 受众分流 + 假说门 + 策略下线）

    Args:
        entry_signals: 买入信号列表（来自 unified_engine）
            [{stock_code, stock_name, entry_type, trigger_price, ...}, ...]
        exit_signals: 卖出信号列表
            [{stock_code, stock_name, exit_type, trigger_price, ...}, ...]
        total_asset: 兼容旧接口；纯信号模式下不参与拦截。
        market_mode: 当前市场模式
        holdings: 回执闭环聚合持仓（传入后启用受众分流：买入事件只对
            空仓者成立，持仓者输出持有/加仓/减仓/止损四选一）
        offline_strategies: 已下线策略列表（【六】滚动50笔期望为负/胜率破线）

    Returns:
        {
            'buy': [ScheduledSignal, ...],              # 空仓者买入事件
            'sell': [ScheduledSignal, ...],             # 卖出信号（全量输出）
            'position_advice': [ScheduledSignal, ...],  # 持仓者四选一
            'skipped': {...}, 'stats': {...}
        }
    """
    stats = {
        'entry_in': len(entry_signals),
        'sell_in': len(exit_signals),
        'sell_skipped_invalid_price': 0,
        'buy_executed': 0, 'sell_executed': 0,
        'buy_skipped_dust_order': 0,
        'buy_skipped_low_confidence': 0,
        'buy_score_gate': 0,
        'buy_strategy_offline': 0,
        'position_advice': 0,
        'point_buy': 0,
        'point_sell': 0,
    }
    skipped = {
        'buy_dust_order': [],
        'buy_low_confidence': [],
        'buy_score_gate': [],
        'buy_strategy_offline': [],
    }
    warnings = {
        'buy_score_gate': [],
    }
    holdings = holdings or []
    held_map = {}
    for h in holdings:
        code = str(h.get('code') or h.get('stock_code') or '')
        if code:
            held_map[code] = h
    offline_set = set(offline_strategies or [])
    point_signals = build_point_scheduled_signals(point_decisions, market_mode)
    stats['point_buy'] = len(point_signals['point_buy'])
    stats['point_sell'] = len(point_signals['point_sell'])
    # ---- 第1步：卖出信号全量输出（信号服务模式：持仓由用户管理，不校验）----
    scheduled_sells = []
    for sig in exit_signals:
        trigger_price = _positive_price(
            sig.get('trigger_price') or sig.get('current_price')
        )
        stop_loss_price = _positive_price(
            sig.get('stop_loss_price') or sig.get('stop_loss')
        )
        if trigger_price <= 0 and stop_loss_price <= 0:
            # 双 0 是数据缺失，不是卖出证据；但有效止损价保留，避免误杀真实信号。
            stats['sell_skipped_invalid_price'] += 1
            logger.warning(
                "卖出信号缺少有效价位，跳过: %s %s",
                sig.get('stock_code', ''), sig.get('exit_type', ''),
            )
            continue
        scheduled_sells.append(ScheduledSignal(
            stock_code=sig.get('stock_code', ''),
            stock_name=sig.get('stock_name', sig.get('stock_code', '')),
            action='sell',
            exit_type=sig.get('exit_type', ''),
            trigger_price=trigger_price,
            stop_loss_price=stop_loss_price,
            reason=sig.get('reason', ''),
            urgency=sig.get('urgency', '常规'),
            market_mode=market_mode,
            schedule_note='卖出信号（信号服务模式，持仓自行核对）',
        ))
        stats['sell_executed'] += 1

    # ---- 第2步：买入调度（策略下线 + 受众分流 + 类型优先级排序）----
    buy_with_prio = []
    for sig in entry_signals:
        entry_type = sig.get('entry_type', '') or _parse_entry_type(sig.get('note', '') or sig.get('reason', ''))
        prio = ENTRY_PRIORITY.get(entry_type, 0)
        buy_with_prio.append((sig, entry_type, prio))

    # 按优先级降序排序；同优先级用 信心→紧急度→代码 兜底，保证调度可复现（P1-1 审计 2026-08-18）
    _URGENCY_RANK = {"紧急": 3, "重要": 2, "常规": 1}
    _CONF_RANK = {"高": 3, "中": 2, "低": 1}

    def _buy_sort_key(item):
        sig, _entry_type, prio = item
        return (-prio,
                -_CONF_RANK.get(str(sig.get('confidence', '中')), 2),
                -_URGENCY_RANK.get(str(sig.get('urgency', '常规')), 1),
                sig.get('stock_code', ''))

    buy_with_prio.sort(key=_buy_sort_key)

    scheduled_buys = []
    position_advices = []
    for sig, entry_type, _prio in buy_with_prio:
        code = sig.get('stock_code', '')
        plan = sig.get('execution_plan') or {}

        # 旧计划/外部信号兜底：评分层 ≤1 时显著提示，不拦截。
        try:
            confidence_score = int(plan.get('confidence_score', -1))
        except (TypeError, ValueError):
            confidence_score = -1
        gate_notes = []
        gate_note_text = ' | '.join(gate_notes)
        if 0 <= confidence_score <= 1:
            gate_reasons = list(plan.get('confidence_details') or [])
            gate_reasons.extend(plan.get('rejection_reasons') or [])
            gate_reason_text = '；'.join(str(item) for item in gate_reasons if str(item))
            gate_reason = (
                f"评分提示: 综合评分{confidence_score}/{plan.get('applicable_score', '?')}，"
                "策略已命中，按规则调度"
            )
            if gate_reason_text:
                gate_reason += f" | {gate_reason_text}"
            gate_notes.append(gate_reason)
            warnings['buy_score_gate'].append({
                'stock_code': code,
                'stock_name': sig.get('stock_name', code),
                'entry_type': entry_type,
                'reason': gate_reason,
                'confidence_details': list(plan.get('confidence_details') or []),
            })
            stats['buy_score_gate'] += 1

        # 【六】策略下线过滤（滚动50笔期望为负/胜率跌破盈亏平衡线）
        if entry_type in offline_set:
            skipped['buy_strategy_offline'].append({
                'stock_code': code,
                'stock_name': sig.get('stock_name', code),
                'entry_type': entry_type,
                'reason': '策略已被业绩判定下线，等待重校',
            })
            stats['buy_strategy_offline'] += 1
            continue

        trigger_price = sig.get('trigger_price', 0) or sig.get('entry_trigger_price', 0)
        if trigger_price <= 0:
            continue
        confidence = str(sig.get('confidence', '中'))
        driver = sig.get('driver') or sig.get('driver', {}) or {}

        # 【三】受众分流：买入事件只对空仓者成立；
        # 持仓者的输出永远是持有/加仓/减仓/止损四选一
        if code in held_map:
            held = held_map[code]
            sector_status = str(sig.get('sector_status') or '')
            no_add = sector_status == 'retreating'
            position_advices.append(ScheduledSignal(
                stock_code=code,
                stock_name=sig.get('stock_name', code),
                action='position',
                entry_type=entry_type,
                trigger_price=trigger_price,
                reason=sig.get('note', '') or sig.get('reason', ''),
                confidence=confidence,
                benchmark_price=float(sig.get('benchmark_price') or plan.get('benchmark_price') or trigger_price),
                rrr_low=float(sig.get('rrr_low') or plan.get('rrr_low') or 0),
                execution_plan=plan,
                market_mode=market_mode,
                audience='holding',
                position_action='不加仓' if no_add else '加仓',
                driver=driver,
                schedule_note=(
                    f'持仓{int(held.get("shares") or 0)}股@{held.get("cost_price", 0):.2f} | '
                    + ('板块退潮，禁止加仓 | ' if no_add else '')
                    + (f'{gate_note_text} | ' if gate_note_text else '')
                    + f'买入事件对持仓者=加仓评估（不是重新买入）| '
                    f'{(driver.get("label") or driver.get("dual") or "")}'
                ),
            ))
            stats['position_advice'] += 1
            continue

        tiers = [tier for tier in plan.get('execution_tiers', []) if tier.get('role') == 'main']
        main_tier = tiers[0] if tiers else {}
        position_price = (
            float(main_tier.get('price') or sig.get('benchmark_price')
                  or plan.get('benchmark_price') or trigger_price)
        )
        industry_multiplier = float(plan.get('industry_multiplier', 1.0) or 1.0)
        if market_mode != 'attack' and sig.get('position_level') == 'heavy':
            industry_multiplier = min(industry_multiplier, 1.0)
        risk_multiplier = (
            industry_multiplier
            * float(plan.get('combined_risk_multiplier', 1.0) or 1.0)
        )
        benchmark_price = float(sig.get('benchmark_price') or plan.get('benchmark_price') or trigger_price)
        rrr_low = float(sig.get('rrr_low') or plan.get('rrr_low') or 0)
        scheduled_buys.append(ScheduledSignal(
            stock_code=code,
            stock_name=sig.get('stock_name', code),
            action='buy',
            entry_type=entry_type,
            trigger_price=trigger_price,
            shares=0,
            reason=sig.get('note', '') or sig.get('reason', ''),
            confidence=sig.get('confidence', '中'),
            benchmark_price=benchmark_price,
            rrr_low=rrr_low,
            risk_multiplier=risk_multiplier,
            industry_multiplier=industry_multiplier,
            execution_plan=plan,
            market_mode=market_mode,
            audience='empty',
            driver=driver,
            schedule_note=(
                (f'{gate_note_text} | ' if gate_note_text else '')
                +
                f'基准{benchmark_price:.2f} | RRR{rrr_low:.2f} | '
                f'止损{sig.get("stop_loss", 0):.2f} | '
                f'产业系数{industry_multiplier:.2f} | '
                f'风险系数{plan.get("combined_risk_multiplier", 1.0):.2f} | '
                f'主档{position_price:.2f} | 信号服务不指定股数'
            ),
        ))
        stats['buy_executed'] += 1

    # 【三】持仓者的四选一之"持有"（无任何买卖信号的持仓股）与
    # "减仓/止损"（出场信号）由 engine.py 观察卡/卖出推送负责，此处只补"加仓"。

    logger.info(
        "实盘信号调度: 买入 %d/%d (策略下线 %d), 持仓建议 %d, 卖出 %d/%d (无效价位 %d), 点位买 %d/卖 %d",
        stats['buy_executed'], stats['entry_in'],
        stats['buy_strategy_offline'],
        stats['position_advice'],
        stats['sell_executed'], stats['sell_in'],
        stats['sell_skipped_invalid_price'],
        len(point_signals['point_buy']), len(point_signals['point_sell']),
    )

    return {
        'buy': scheduled_buys,
        'sell': scheduled_sells,
        'position_advice': position_advices,
        'point_buy': point_signals['point_buy'],
        'point_sell': point_signals['point_sell'],
        'skipped': skipped,
        'warnings': warnings,
        'stats': stats,
    }


def format_scheduled_summary(scheduled: Dict[str, Any]) -> str:
    """格式化调度结果为可推送的文本摘要"""
    lines = []
    stats = scheduled['stats']
    lines.append("📊 信号调度统计:")
    lines.append(
        f"  买入: {stats['buy_executed']}/{stats['entry_in']} "
        f"(策略下线 {stats.get('buy_strategy_offline', 0)}, "
        f"策略下线 {stats.get('buy_strategy_offline', 0)}, "
        f"低置信度转观察 {stats['buy_skipped_low_confidence']})"
    )
    lines.append(
        f"  卖出: {stats['sell_executed']}/{stats['sell_in']} "
        f"(无效价位 {stats.get('sell_skipped_invalid_price', 0)})"
    )
    if stats.get('position_advice'):
        lines.append(f"  持仓建议(加仓评估): {stats['position_advice']}")
    lines.append("")

    if scheduled['sell']:
        lines.append("🔴 卖出信号（全量输出，持仓自行核对）:")
        for s in scheduled['sell']:
            lines.append(f"  {s.stock_code} {s.stock_name} | {s.exit_type} @ {s.trigger_price:.2f} | {s.urgency}")
            lines.append(f"    {s.reason[:80]}")
        lines.append("")

    point_buys = scheduled.get('point_buy') or []
    if point_buys:
        lines.append("📐 独立点表买入（原策略不变）:")
        for s in point_buys:
            lines.append(
                f"  {s.stock_code} {s.stock_name} | {s.entry_type} @ {s.trigger_price:.2f} | {s.shares}股"
            )
            lines.append(f"    {s.schedule_note}")
        lines.append("")

    point_sells = scheduled.get('point_sell') or []
    if point_sells:
        lines.append("🧯 独立点表卖出（原策略不变）:")
        for s in point_sells:
            lines.append(
                f"  {s.stock_code} {s.stock_name} | {s.exit_type} @ {s.trigger_price:.2f} | {s.action_size:.0%}"
            )
            lines.append(f"    {s.schedule_note}")
        lines.append("")

    if scheduled.get('position_advice'):
        lines.append("🟡 持仓建议（四选一，不对空仓者成立）:")
        for s in scheduled['position_advice']:
            lines.append(
                f"  {s.stock_code} {s.stock_name} | {s.position_action} | "
                f"{s.entry_type} @ {s.trigger_price:.2f}"
            )
            lines.append(f"    {s.schedule_note[:80]}")
        lines.append("")

    if scheduled['buy']:
        lines.append("🟢 买入信号（按类型优先级排序，仅对空仓者成立）:")
        for s in scheduled['buy']:
            lines.append(f"  {s.stock_code} {s.stock_name} | {s.entry_type} @ {s.trigger_price:.2f} | {s.shares}股")
            lines.append(f"    {s.schedule_note}")
        lines.append("")

    offline_skipped = scheduled.get('skipped', {}).get('buy_strategy_offline') or []
    if offline_skipped:
        lines.append("⛔ 策略下线过滤:")
        for r in offline_skipped:
            lines.append(f"  {r['stock_code']} {r['stock_name']} | {r['entry_type']}: {r['reason']}")
        lines.append("")

    score_warnings = (scheduled.get('warnings') or {}).get('buy_score_gate') or []
    if score_warnings:
        lines.append("⚠️ 评分提示（逐条，不拦截）:")
        for r in score_warnings:
            lines.append(f"  {r['stock_code']} {r['stock_name']} | {r['entry_type']}: {r['reason'][:180]}")
        lines.append("")

    return '\n'.join(lines)
