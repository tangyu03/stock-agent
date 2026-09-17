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
# 【仓位层→账户层】预算绝对值显式化：预算基数与账户口径不再藏在
# 模块常量里（蘅东光 9/8 验收：300股×88.56=26,568 单笔风险敞口，按 1%
# 风险预算反推隐含 265 万账户——公式对了，绝对值是黑箱）。全部走
# config/timing.yaml position_budget，推送/参数附录一并披露。
def _position_budget_cfg() -> Dict:
    try:
        from ..config_models import load_config
        timing = (load_config("timing.yaml") or {}).get("timing", {}) or {}
        return (timing or {}).get("position_budget") or {}
    except Exception:
        return {}


def _budget_per_stock() -> float:
    return float(_position_budget_cfg().get("budget_per_stock", 250_000))


def _account_value() -> float:
    return float(_position_budget_cfg().get("account_value", 1_000_000))


def _budget_disclosure_note(
    shares: int,
    position_price: float,
    stop_price: float,
    risk_budget_pct: float = 0.01,
) -> str:
    """建议仓位的可审计披露：金额/单笔风险敞口/账户口径。

    蘅东光 9/8 审计实证：300股×单股风险88.56=26,568 元敞口，若按
    risk_budget_pct(1%) 反推隐含账户 = 26,568÷1% ≈ 265 万——此前预算
    基数 25 万与 1% 风险预算并存，账户绝对值从不披露。
    """
    budget = _budget_per_stock()
    account = _account_value()
    amount = shares * position_price
    risk_per_share = position_price - stop_price
    risk_exposure = shares * risk_per_share if risk_per_share > 0 else None
    parts = [
        f"建议{shares:,}股×{position_price:.2f}={amount:,.0f}元(预算基数{budget/10000:.0f}万)"
    ]
    if risk_exposure is not None:
        parts.append(f"单笔风险敞口{risk_exposure:,.0f}元({shares:,}股×{risk_per_share:.2f})")
        implied = risk_exposure / risk_budget_pct if risk_budget_pct > 0 else None
        pct_of_account = risk_exposure / account * 100 if account > 0 else None
        parts.append(
            f"账户{account/10000:.0f}万(1%预算={account*risk_budget_pct:,.0f}元，"
            f"敞口占{pct_of_account:.1f}%)" if pct_of_account is not None
            else f"账户{account/10000:.0f}万"
        )
        if implied is not None:
            parts.append(f"按{risk_budget_pct*100:.0f}%风险预算反推隐含账户{implied/10000:.0f}万")
    return " | ".join(parts)


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
    # 【三】受众：empty=空仓者买入 / holding=持仓者建议
    audience: str = 'empty'
    # 【三】持仓四选一动作：持有/加仓/减仓/止损（action='position' 时有效）
    position_action: str = ''
    # 【一】假说（推送给模板/落库用）
    hypothesis: dict = field(default_factory=dict)


def _parse_entry_type(reason: str) -> str:
    """从 reason 解析 entry_type"""
    if not reason:
        return ''
    # reason 格式: "价量突破: 站上MA25..." 或 "MA5压制: ..."
    if ':' in reason:
        return reason.split(':', 1)[0].strip()
    return reason.strip()


def schedule_live_signals(
    entry_signals: List[Dict],
    exit_signals: List[Dict],
    total_asset: float = 1_000_000,
    market_mode: str = 'defend',
    holdings: Optional[List[Dict]] = None,
    offline_strategies: Optional[List[str]] = None,
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
        'buy_skipped_no_budget': 0,
        'buy_skipped_dust_order': 0,
        'buy_skipped_low_confidence': 0,
        'buy_hypothesis_rejected': 0,
        'buy_score_gate': 0,
        'buy_ev_gate': 0,
        'buy_strategy_offline': 0,
        'position_advice': 0,
    }
    skipped = {
        'buy_no_budget': [],
        'buy_dust_order': [],
        'buy_low_confidence': [],
        'buy_hypothesis_rejected': [],
        'buy_score_gate': [],
        'buy_ev_gate': [],
        'buy_strategy_offline': [],
    }
    holdings = holdings or []
    held_map = {}
    for h in holdings:
        code = str(h.get('code') or h.get('stock_code') or '')
        if code:
            held_map[code] = h
    offline_set = set(offline_strategies or [])
    budget_per_stock = _budget_per_stock()
    account_value = _account_value()

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

    # ---- 第2步：买入调度（假说门 + 策略下线 + 受众分流 + 类型优先级排序）----
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

        # 旧计划/外部信号兜底：评分层 ≤1 时不得进入买入区。
        # 新计划已带 ev_gate；这里保证历史计划也不能绕过输出端闸门。
        try:
            confidence_score = int(plan.get('confidence_score', -1))
        except (TypeError, ValueError):
            confidence_score = -1
        if not plan.get('ev_gate') and 0 <= confidence_score <= 1:
            skipped['buy_score_gate'].append({
                'stock_code': code,
                'stock_name': sig.get('stock_name', code),
                'entry_type': entry_type,
                'reason': f'评分闸门: 综合评分{confidence_score}≤1，已触发未放行',
            })
            stats['buy_score_gate'] += 1
            continue

        # 【评分层→决策层】期望值闸门（防御纵深：0/6 低置信/EV<0 不进调度）
        ev_verdict = plan.get('ev_gate') or {}
        if ev_verdict.get('rejected'):
            skipped['buy_ev_gate'].append({
                'stock_code': code,
                'stock_name': sig.get('stock_name', code),
                'entry_type': entry_type,
                'reason': str(ev_verdict.get('reason') or '期望值闸门拒绝'),
            })
            stats['buy_ev_gate'] += 1
            continue

        # 【一】假说门（防御纵深：timing_engine 已在生成阶段拒绝，此处兜底）
        if plan.get('execute') is False or plan.get('hypothesis_rejected'):
            skipped['buy_hypothesis_rejected'].append({
                'stock_code': code,
                'stock_name': sig.get('stock_name', code),
                'entry_type': entry_type,
                'reason': '; '.join(plan.get('rejection_reasons') or ['假说不完整']),
            })
            stats['buy_hypothesis_rejected'] += 1
            continue

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
        hypothesis = sig.get('hypothesis') or plan.get('hypothesis') or {}

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
                hypothesis=hypothesis,
                schedule_note=(
                    f'持仓{int(held.get("shares") or 0)}股@{held.get("cost_price", 0):.2f} | '
                    + ('板块退潮，禁止加仓 | ' if no_add else '')
                    + f'买入事件对持仓者=加仓评估（不是重新买入）| '
                    f'假说: {hypothesis.get("sentence", "")}'
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
        shares = int((budget_per_stock / position_price * risk_multiplier) // 100) * 100
        base_shares = int((budget_per_stock / position_price) // 100) * 100

        # 【P1-1】防守模式确认追强降仓放行：试探仓比例 = 单笔风险预算 ÷ 止损距离
        # （出处是风险预算，不是惯例：中波动 1%÷3.5%≈29%≈1/3；
        #   蘅东光类高波动标的止损距离~10% → 仓位~10%）。
        defensive_chase_flag = bool(
            sig.get('defensive_chase') or (hypothesis or {}).get('defensive_chase')
        )
        if market_mode == 'defend' and defensive_chase_flag and position_price > 0:
            try:
                from ..analyzers.signal_plan import compute_risk_budget_position
                _z = float((hypothesis or {}).get('z') or sig.get('stop_loss') or 0)
                _dc_config = None
                try:
                    from ..analyzers.timing_engine import get_timing_engine
                    _dc_config = {'defensive_chase': get_timing_engine()._cfg('defensive_chase') or {}}
                except Exception:
                    _dc_config = None
                budget = compute_risk_budget_position(position_price, _z, config=_dc_config)
                shares = int((budget_per_stock * budget['ratio'] / position_price) // 100) * 100
                if shares <= 0:
                    # 试探仓地板：预算仓位不足 1 手时按 1 手试探
                    # （比例语义保留在 risk_budget_note，仓位出处可审计）
                    shares = 100
                # 【仓位层→账户层】风险预算 note 追加敞口/账户披露
                # （蘅东光 9/8：300股×88.56=26,568 敞口→隐含 265 万账户，
                #   绝对值不再黑箱）
                _budget_pct = float(budget.get('budget_pct') or 0.01)
                _disclosure = _budget_disclosure_note(
                    shares, position_price, _z, _budget_pct,
                )
                _budget_note = f"{budget['note']} | {_disclosure}"
                plan['risk_budget_note'] = _budget_note
                plan['defensive_chase'] = True
                for _tier in plan.get('execution_tiers', []):
                    _tier['risk_budget_note'] = _budget_note
                sig['defensive_chase'] = True  # 统计分层键：确认追强@防守
                if isinstance(hypothesis, dict):
                    hypothesis['defensive_chase'] = True   # 落库假说携带标记
            except Exception as exc:
                logger.debug('风险预算仓位计算失败（回退常规乘数）: %s', str(exc)[:60])
        plan['base_shares'] = base_shares
        if shares <= 0:
            continue
        plan['suggested_shares'] = shares
        for tier in plan.get('execution_tiers', []):
            if tier.get('role') == 'main':
                tier['base_shares'] = base_shares
            elif tier.get('role') == 'probe':
                tier['base_shares'] = int((base_shares / 3) // 100) * 100
            else:
                tier['base_shares'] = 0
        benchmark_price = float(sig.get('benchmark_price') or plan.get('benchmark_price') or trigger_price)
        rrr_low = float(sig.get('rrr_low') or plan.get('rrr_low') or 0)
        scheduled_buys.append(ScheduledSignal(
            stock_code=code,
            stock_name=sig.get('stock_name', code),
            action='buy',
            entry_type=entry_type,
            trigger_price=trigger_price,
            shares=shares,
            reason=sig.get('note', '') or sig.get('reason', ''),
            confidence=sig.get('confidence', '中'),
            benchmark_price=benchmark_price,
            rrr_low=rrr_low,
            risk_multiplier=risk_multiplier,
            industry_multiplier=industry_multiplier,
            execution_plan=plan,
            market_mode=market_mode,
            audience='empty',
            hypothesis=hypothesis,
            schedule_note=(
                f'基准{benchmark_price:.2f} | RRR{rrr_low:.2f} | '
                f'Z认错{hypothesis.get("z", 0) or sig.get("stop_loss", 0):.2f} | '
                f'产业系数{industry_multiplier:.2f} | '
                f'风险系数{plan.get("combined_risk_multiplier", 1.0):.2f} | '
                f'主档{position_price:.2f} | 建议{shares}股 | '
                + _budget_disclosure_note(
                    shares, position_price,
                    float(hypothesis.get("z") or sig.get("stop_loss") or position_price),
                )
            ),
        ))
        stats['buy_executed'] += 1

    # 【三】持仓者的四选一之"持有"（无任何买卖信号的持仓股）与
    # "减仓/止损"（出场信号）由 engine.py 观察卡/卖出推送负责，此处只补"加仓"。

    logger.info(
        "实盘信号调度: 买入 %d/%d (假说拒 %d, 策略下线 %d), 持仓建议 %d, 卖出 %d/%d (无效价位 %d)",
        stats['buy_executed'], stats['entry_in'],
        stats['buy_hypothesis_rejected'], stats['buy_strategy_offline'],
        stats['position_advice'],
        stats['sell_executed'], stats['sell_in'],
        stats['sell_skipped_invalid_price'],
    )

    return {
        'buy': scheduled_buys,
        'sell': scheduled_sells,
        'position_advice': position_advices,
        'skipped': skipped,
        'stats': stats,
    }


def format_scheduled_summary(scheduled: Dict[str, Any]) -> str:
    """格式化调度结果为可推送的文本摘要"""
    lines = []
    stats = scheduled['stats']
    lines.append("📊 信号调度统计:")
    lines.append(
        f"  买入: {stats['buy_executed']}/{stats['entry_in']} "
        f"(假说拒 {stats.get('buy_hypothesis_rejected', 0)}, "
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
            hyp = s.hypothesis or {}
            if hyp.get('sentence'):
                lines.append(f"    假说: {hyp['sentence']}")
        lines.append("")

    rejected = scheduled.get('skipped', {}).get('buy_hypothesis_rejected') or []
    if rejected:
        lines.append("⛔ 假说不完整被拒（可证伪性出厂检查）:")
        for r in rejected:
            lines.append(f"  {r['stock_code']} {r['stock_name']} | {r['entry_type']}: {r['reason'][:90]}")
        lines.append("")

    offline_skipped = scheduled.get('skipped', {}).get('buy_strategy_offline') or []
    if offline_skipped:
        lines.append("⛔ 策略下线过滤:")
        for r in offline_skipped:
            lines.append(f"  {r['stock_code']} {r['stock_name']} | {r['entry_type']}: {r['reason']}")
        lines.append("")

    return '\n'.join(lines)
