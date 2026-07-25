"""
P3: 实盘信号调度器 — Step0 逻辑移植到实盘
==========================================
将回测层的 Step0 信号调度器（持仓跟踪+期望排序+预算约束）移植到实盘 orchestrator。

与回测层 SignalScheduler 的区别：
- 回测层：维护模拟持仓状态，按日推进
- 实盘层：从 portfolio.yaml.add_plans 读取真实持仓状态，单次调度

核心逻辑（与 Step0 一致）：
1. 卖出优先：先处理卖出信号（释放资金）
2. 买入按期望排序：价量突破(+2.92) > 套利低吸(+1.30) > 恐慌抄底(+0.66) > 确认追强(0)
3. 预算约束：单股最大仓位25万、最大并发4只
4. 主动放弃：期望低于 -0.5% 的买入信号直接丢弃

使用方式：
    from src.decision.live_scheduler import schedule_live_signals
    scheduled = schedule_live_signals(entry_signals, exit_signals, holdings, total_asset)
    # scheduled = {'buy': [...], 'sell': [...], 'skipped': {...}}
"""
import logging
from typing import Dict, List, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ============================================================
# 调度参数（与 Step0 / position.yaml 一致）
# ============================================================
BUDGET_PER_STOCK = 250_000      # 单股最大仓位
SINGLE_STOCK_MAX = 0.25         # 单票上限 25%
MAX_CONCURRENT = 4              # 最大并发持仓
EXPECTANCY_FLOOR = -0.5         # 期望低于此阈值主动放弃

# A6 计算的各 entry_type 期望值（%/笔）
# 来源：阶段一 A6 分仓位类型报告
ENTRY_EXPECTANCY = {
    '价量突破': 2.9165,
    '套利低吸': 1.2955,
    '恐慌抄底': 0.6610,
    '确认追强': 0.0,  # 样本不足，按0处理
}


@dataclass
class ScheduledSignal:
    """调度后信号"""
    stock_code: str
    stock_name: str
    action: str  # 'buy' / 'sell'
    entry_type: str = ''
    exit_type: str = ''
    trigger_price: float = 0.0
    shares: int = 0
    reason: str = ''
    urgency: str = '常规'
    confidence: str = '中'
    expectancy: float = 0.0
    market_mode: str = 'defend'
    # 调度信息
    schedule_note: str = ''  # 调度原因说明


def _parse_entry_type(reason: str) -> str:
    """从 reason 解析 entry_type"""
    if not reason:
        return ''
    # reason 格式: "价量突破: 站上MA25..." 或 "MA5压制: ..."
    if ':' in reason:
        return reason.split(':', 1)[0].strip()
    return reason.strip()


def _get_holding_info(holdings: List[Dict], code: str) -> Dict:
    """从持仓列表获取指定股票的持仓信息"""
    for h in holdings:
        if h.get('code') == code or h.get('stock_code') == code:
            return h
    return {}


def schedule_live_signals(
    entry_signals: List[Dict],
    exit_signals: List[Dict],
    holdings: List[Dict],
    total_asset: float = 1_000_000,
    market_mode: str = 'defend',
) -> Dict[str, Any]:
    """
    实盘信号调度器

    Args:
        entry_signals: 买入信号列表（来自 unified_engine）
            [{stock_code, stock_name, entry_type, trigger_price, ...}, ...]
        exit_signals: 卖出信号列表
            [{stock_code, stock_name, exit_type, trigger_price, ...}, ...]
        holdings: 当前持仓列表（来自 portfolio.yaml 或 trade_logger）
            [{code, shares, cost_price, ...}, ...]
        total_asset: 总资产（用于计算可用资金）
        market_mode: 当前市场模式

    Returns:
        {
            'buy': [ScheduledSignal, ...],     # 调度后保留的买入信号
            'sell': [ScheduledSignal, ...],    # 调度后保留的卖出信号
            'skipped': {
                'buy_max_concurrent': [...],
                'buy_already_holding': [...],
                'buy_low_expectancy': [...],
                'buy_no_budget': [...],
            },
            'stats': {
                'entry_in': int, 'sell_in': int,
                'buy_executed': int, 'sell_executed': int,
                'buy_skipped_max_concurrent': int,
                'buy_skipped_already_holding': int,
                'buy_skipped_low_expectancy': int,
                'buy_skipped_no_budget': int,
            }
        }
    """
    # 计算当前持仓数和已用资金
    current_holdings = {h.get('code') or h.get('stock_code', ''): h for h in holdings if h.get('code') or h.get('stock_code')}
    n_holding = len(current_holdings)
    used_cash = sum(
        (h.get('shares', 0)) * (h.get('cost_price', 0))
        for h in current_holdings.values()
    )
    available_cash = max(0, total_asset - used_cash)

    stats = {
        'entry_in': len(entry_signals),
        'sell_in': len(exit_signals),
        'buy_executed': 0, 'sell_executed': 0,
        'buy_skipped_max_concurrent': 0,
        'buy_skipped_already_holding': 0,
        'buy_skipped_low_expectancy': 0,
        'buy_skipped_no_budget': 0,
    }
    skipped = {
        'buy_max_concurrent': [],
        'buy_already_holding': [],
        'buy_low_expectancy': [],
        'buy_no_budget': [],
    }

    # ---- 第1步：卖出优先（释放资金）----
    scheduled_sells = []
    for sig in exit_signals:
        code = sig.get('stock_code', '')
        if code not in current_holdings:
            # 无持仓，跳过卖出信号
            continue
        scheduled_sells.append(ScheduledSignal(
            stock_code=code,
            stock_name=sig.get('stock_name', code),
            action='sell',
            exit_type=sig.get('exit_type', ''),
            trigger_price=sig.get('trigger_price', 0),
            reason=sig.get('reason', ''),
            urgency=sig.get('urgency', '常规'),
            market_mode=market_mode,
            schedule_note='卖出释放资金',
        ))
        stats['sell_executed'] += 1
        # 卖出后释放资金（估算）
        h = current_holdings[code]
        released = h.get('shares', 0) * sig.get('trigger_price', 0)
        available_cash += released * 0.999  # 扣佣金+印花税
        # 从持仓中移除（仅本次调度内）
        del current_holdings[code]
        n_holding -= 1

    # ---- 第2步：买入按期望排序 ----
    buy_with_exp = []
    for sig in entry_signals:
        code = sig.get('stock_code', '')
        entry_type = sig.get('entry_type', '') or _parse_entry_type(sig.get('note', '') or sig.get('reason', ''))
        exp = ENTRY_EXPECTANCY.get(entry_type, 0.0)
        buy_with_exp.append((sig, entry_type, exp))

    # 按期望降序排序
    buy_with_exp.sort(key=lambda x: -x[2])

    scheduled_buys = []
    for sig, entry_type, exp in buy_with_exp:
        code = sig.get('stock_code', '')

        # 已持仓则跳过
        if code in current_holdings:
            skipped['buy_already_holding'].append({
                'stock_code': code, 'entry_type': entry_type, 'reason': '已持仓'
            })
            stats['buy_skipped_already_holding'] += 1
            continue

        # 期望过低主动放弃
        if exp < EXPECTANCY_FLOOR:
            skipped['buy_low_expectancy'].append({
                'stock_code': code, 'entry_type': entry_type, 'expectancy': exp
            })
            stats['buy_skipped_low_expectancy'] += 1
            continue

        # 并发上限
        if n_holding >= MAX_CONCURRENT:
            skipped['buy_max_concurrent'].append({
                'stock_code': code, 'entry_type': entry_type,
                'reason': f'并发上限{MAX_CONCURRENT}已满'
            })
            stats['buy_skipped_max_concurrent'] += 1
            continue

        # 预算检查
        trigger_price = sig.get('trigger_price', 0) or sig.get('entry_trigger_price', 0)
        if trigger_price <= 0:
            continue
        shares = int((BUDGET_PER_STOCK / trigger_price) // 100) * 100
        if shares <= 0:
            continue
        amount = trigger_price * shares
        cost = amount * 0.00025  # 佣金
        total_deduction = amount + cost

        if total_deduction > available_cash:
            # 资金不足，尝试缩减股数
            affordable = int((available_cash / (trigger_price * (1 + 0.00025))) // 100) * 100
            if affordable <= 0:
                skipped['buy_no_budget'].append({
                    'stock_code': code, 'entry_type': entry_type,
                    'need': total_deduction, 'available': available_cash
                })
                stats['buy_skipped_no_budget'] += 1
                continue
            max_by_budget = int((BUDGET_PER_STOCK / trigger_price) // 100) * 100
            shares = min(affordable, max_by_budget)
            if shares <= 0:
                skipped['buy_no_budget'].append({
                    'stock_code': code, 'entry_type': entry_type,
                    'reason': '缩减后仍不足'
                })
                stats['buy_skipped_no_budget'] += 1
                continue
            total_deduction = trigger_price * shares * (1 + 0.00025)

        # 执行买入
        scheduled_buys.append(ScheduledSignal(
            stock_code=code,
            stock_name=sig.get('stock_name', code),
            action='buy',
            entry_type=entry_type,
            trigger_price=trigger_price,
            shares=shares,
            reason=sig.get('note', '') or sig.get('reason', ''),
            confidence=sig.get('confidence', '中'),
            expectancy=exp,
            market_mode=market_mode,
            schedule_note=f'期望{exp:+.2f}%，分配{shares}股',
        ))
        stats['buy_executed'] += 1
        available_cash -= total_deduction
        # 模拟加入持仓
        current_holdings[code] = {'shares': shares, 'cost_price': trigger_price}
        n_holding += 1

    logger.info(
        "实盘信号调度: 买入 %d/%d (跳过: 并发%d/已持仓%d/低期望%d/无资金%d), 卖出 %d/%d",
        stats['buy_executed'], stats['entry_in'],
        stats['buy_skipped_max_concurrent'], stats['buy_skipped_already_holding'],
        stats['buy_skipped_low_expectancy'], stats['buy_skipped_no_budget'],
        stats['sell_executed'], stats['sell_in'],
    )

    return {
        'buy': scheduled_buys,
        'sell': scheduled_sells,
        'skipped': skipped,
        'stats': stats,
    }


def format_scheduled_summary(scheduled: Dict[str, Any]) -> str:
    """格式化调度结果为可推送的文本摘要"""
    lines = []
    stats = scheduled['stats']
    lines.append(f"📊 信号调度统计:")
    lines.append(f"  买入: {stats['buy_executed']}/{stats['entry_in']} (跳过: 并发{stats['buy_skipped_max_concurrent']}/已持仓{stats['buy_skipped_already_holding']}/低期望{stats['buy_skipped_low_expectancy']}/无资金{stats['buy_skipped_no_budget']})")
    lines.append(f"  卖出: {stats['sell_executed']}/{stats['sell_in']}")
    lines.append("")

    if scheduled['sell']:
        lines.append("🔴 卖出信号（优先执行，释放资金）:")
        for s in scheduled['sell']:
            lines.append(f"  {s.stock_code} {s.stock_name} | {s.exit_type} @ {s.trigger_price:.2f} | {s.urgency}")
            lines.append(f"    {s.reason[:80]}")
        lines.append("")

    if scheduled['buy']:
        lines.append("🟢 买入信号（按期望排序，预算约束）:")
        for s in scheduled['buy']:
            lines.append(f"  {s.stock_code} {s.stock_name} | {s.entry_type} @ {s.trigger_price:.2f} | {s.shares}股 | 期望{s.expectancy:+.2f}%")
            lines.append(f"    {s.schedule_note}")
        lines.append("")

    skipped = scheduled['skipped']
    if any(skipped.values()):
        lines.append("⚠️ 跳过信号:")
        if skipped['buy_max_concurrent']:
            lines.append(f"  并发上限: {[s['stock_code'] for s in skipped['buy_max_concurrent']]}")
        if skipped['buy_already_holding']:
            lines.append(f"  已持仓: {[s['stock_code'] for s in skipped['buy_already_holding']]}")
        if skipped['buy_low_expectancy']:
            lines.append(f"  低期望: {[(s['stock_code'], s['entry_type']) for s in skipped['buy_low_expectancy']]}")
        if skipped['buy_no_budget']:
            lines.append(f"  无资金: {[s['stock_code'] for s in skipped['buy_no_budget']]}")

    return '\n'.join(lines)
