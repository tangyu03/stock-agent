from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _text(value: Any) -> str:
    return str(value if value is not None else '').strip()


def _price(value: Any) -> str:
    try:
        return f'{float(value):g}'
    except (TypeError, ValueError):
        return ''


def render_header(market: Mapping[str, Any]) -> str:
    amount = market.get('amount_yi')
    tier = str(market.get('tier', '')).upper()
    parts: list[str] = []
    if amount is not None and tier == 'LOW':
        parts.append(f'{_price(amount)}亿，这个量架构不起行情，只架构得起轮动。')
    elif amount is not None and tier == 'HIGH':
        parts.append(f'久违的{_price(amount)}亿。')
    close = market.get('sh_close')
    ma5 = market.get('sh_ma5')
    if close is not None and ma5 is not None and float(ma5):
        distance = abs(float(close) - float(ma5)) / float(ma5)
        if distance < 0.005:
            position = '上方' if float(close) >= float(ma5) else '下方'
            parts.append(
                f'收{_price(close)}，在MA5（{_price(ma5)}）{position}'
                f'{distance * 100:.2f}%，明天这根线就是楚河汉界。'
            )
        elif float(close) < float(ma5):
            parts.append(
                f'收{_price(close)}，已破MA5（{_price(ma5)}）'
                f'{distance * 100:.2f}%，明天别幻想。'
            )
    crowded = _text(market.get('crowded_sector'))
    if crowded:
        parts.append(f'{crowded}的成交占比再次拥挤。')
    if market.get('sector_down') and bool(market.get('pool_reversal')):
        parts.append('资金总要找个宣泄口。')
    return ' '.join(parts)


def render_index_points(market: Mapping[str, Any]) -> str:
    rows = list(market.get('index_points') or [])
    if not rows:
        return ''
    rendered = []
    for row in rows:
        name = _text(row.get('name') or '指数')
        levels = '，'.join(_price(level) for level in row.get('levels') or [])
        rendered.append(f'{name}：{levels}' if levels else f'{name}：先看能不能止跌')
    return '\n'.join(['指数点位：'] + rendered)


def render_position(position: Mapping[str, Any]) -> str:
    name = _text(position.get('name') or position.get('symbol'))
    parts: list[str] = []
    if position.get('current_price') is not None:
        parts.append('当前价' + _price(position.get('current_price')) + '。')
    progress = position.get('progress_pct')
    parts.append(f'信号生效至今 +{_price(progress)}%。' if progress is not None else '系统外持仓。')
    if bool(position.get('up_day')) and float(position.get('volume_ratio') or 0) > 1.2 and float(position.get('active_delta') or 0) > 5:
        parts.append('带量的阳线，外盘占优。')
    elif int(position.get('consecutive_down') or 0) >= 3 and float(position.get('volume_ratio') or 1) < 0.9:
        parts.append('缩量阴跌。')
    if int(position.get('volume_tier_gap') or 0) >= 2:
        parts.append('量比和换手两个口径打架。')
    if bool(position.get('margin_eligible')) and int(position.get('margin_down_days') or 0) > 0:
        parts.append('融资余额连降' + str(int(position['margin_down_days'])) + '日。')
    if int(position.get('main_outflow_days') or 0) >= 3:
        parts.append('主力净流出还在扩大。')
    premise = _text(position.get('premise'))
    if premise:
        parts.append(premise + '。')
    pool_size = int(position.get('pool_size') or 0)
    pool_rank = int(position.get('pool_rank') or 0)
    if pool_size >= 2 and pool_rank > 0:
        parts.append('去弱择强，它就是那个强。' if pool_rank == 1 else '跟风位，有龙头在要它干嘛。')
    if position.get('receipt_coverage') is not None and float(position['receipt_coverage']) >= 0.5 and position.get('missed_stop_date'):
        parts.append('早该在' + _text(position['missed_stop_date']) + '那天走，之后每一天都是在给侥幸交税。')
    entry_low = _price(position.get('entry_low'))
    entry_high = _price(position.get('entry_high'))
    if entry_low:
        parts.append(f'先手买入{entry_low}–{entry_high}。')
    stop_low = _price(position.get('stop_low'))
    stop_high = _price(position.get('stop_high'))
    if stop_low:
        stop_prefix = '止损放宽到10日线' if int(position.get('stop_n') or 0) == 10 else '止损'
        parts.append(f'{stop_prefix}{stop_low}' + (f'–{stop_high}' if stop_high else '') + '。')
    profit_low = _price(position.get('profit_low'))
    profit_high = _price(position.get('profit_high'))
    if profit_low:
        parts.append(f'目标卖出{profit_low}–{profit_high}。')
    t_low = _price(position.get('t_buy_low'))
    t_high = _price(position.get('t_buy_high'))
    t_sell = _price(position.get('t_sell_low'))
    if t_low:
        line = f'{t_low}' + (f'–{t_high}' if t_high else '') + '低吸做T'
        if t_sell:
            line += f'，{t_sell}高抛'
        parts.append(line + '。')
        parts.append('T仓位选择：同等 / 加倍 / 减半。')
    sentiment = _sentiment(position)
    if sentiment:
        parts.append(sentiment)
    return f'{name}：' + ''.join(parts)


def _sentiment(position: Mapping[str, Any]) -> str:
    progress = float(position.get('progress_pct') or 0)
    trend = str(position.get('trend', '')).upper()
    premise = _text(position.get('premise'))
    if progress > 15 and trend == 'UP':
        return '这票是拿来格局的，不是拿来折腾的。'
    if int(position.get('main_outflow_days') or 0) >= 3 and trend == 'DOWN':
        return '这个不叫持仓，叫挂账。'
    if any(keyword in premise for keyword in ('套现', '定增')) and int(position.get('consecutive_down') or 0) >= 5:
        return '14只里最没有格局价值的一只。'
    if float(position.get('turnover_p90_ratio') or 0) > 1 and trend == 'UP':
        return '过热了，先出来看。'
    if trend == 'DOWN' and '业绩预增' in premise:
        return '跌是情绪杀不是逻辑杀，但纪律是纪律。'
    if float(position.get('pe') or 0) > 300 and float(position.get('amplitude_pct') or 0) > 5:
        return '情绪票，别格局。'
    if int(position.get('consecutive_down') or 0) >= 5:
        return '破位的不如没破位的，现在只有观察价值。'
    return ''


def render_plan(market: Mapping[str, Any], t_symbols: Iterable[str] = ()) -> str:
    lines: list[str] = []
    level = _text(market.get('index_level'))
    if level:
        lines.append(f'1）开盘先看{level}，破了今天所有宽容全部收回。')
    lines.append('2）破止损的执行，不讨论、不还价。止损是纪律的核心，没有之一。')
    symbols = [(_text(name) or '未知') for name in t_symbols]
    if symbols:
        lines.append('3）做T只碰' + '、'.join(symbols) + '，其他票别硬做。')
    else:
        lines.append('3）明天没有能做T的票。')
    if bool(market.get('zero_plan_day')):
        amount = market.get('amount_yi')
        amount_text = f'{_price(amount)}亿的市场，' if amount is not None else ''
        lines.append(f'4）不开新仓。{amount_text}空仓和轻仓都是合法持仓。')
    else:
        lines.append('4）先手点位已出，尾盘见。')
    return '\n'.join(lines)


def render_closing(market: Mapping[str, Any], receipt_coverage: float | None) -> str:
    if receipt_coverage is not None and receipt_coverage >= 0.5 and bool(market.get('missed_stop_hit')):
        return '亏了别去找逻辑安慰自己。逻辑是用来辅助上涨的，不是用来强化扛单信念的。市场永远是对的。'
    if str(market.get('tier', '')).upper() == 'LOW':
        return '少动就是赢。'
    return ''


def render_execution_review(rows: Iterable[Mapping[str, Any]]) -> str:
    items = list(rows)
    if not items:
        return '【今日执行回顾】：今日无 TRIGGERED 信号。'
    lines = ['【今日执行回顾】：']
    for row in items:
        verdict = _text(row.get('verdict')) or '未回执'
        difference = _text(row.get('difference'))
        suffix = f'，差异：{difference}' if difference else ''
        lines.append(f"{_text(row.get('symbol'))} {_text(row.get('strategy_id'))} {_text(row.get('tag'))} {_price(row.get('zone_low'))}–{_price(row.get('zone_high'))} 回执：{verdict}{suffix}")
    return '\n'.join(lines)


def render_ev_negative(market: Mapping[str, Any]) -> str:
    strategies = list(market.get('ev_negative_strategies') or [])
    if not strategies:
        return ''
    return '【EV_NEG 记录】：' + '、'.join(str(item) for item in strategies) + '（仅记录，不推送预警）。'


def render_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
) -> str:
    lines = [f'【{report_date} 复盘】', render_header(market)]
    ev_negative = render_ev_negative(market)
    if ev_negative:
        lines.append(ev_negative)
    execution_review = list(market.get('execution_review') or [])
    if execution_review:
        lines.append(render_execution_review(execution_review))
    if bool(market.get('zero_plan_day')):
        protected = [position for position in positions if position.get('stop_low') is not None]
        lines.append(f'【持仓】（{len(protected)}只，零预案日仅展示保护行）：')
        lines.extend([render_position(position) for position in protected])
        index_points = render_index_points(market)
        if index_points:
            lines.append(index_points)
        lines.append('【明日计划】：明日零预案，不开新仓。')
        closing = render_closing(market, market.get('receipt_coverage'))
        if closing:
            lines.append(closing)
        lines.append('个人观点，仅供参考。')
        return '\n'.join(lines)
    indexed = list(enumerate(positions))
    ordered = sorted(
        indexed,
        key=lambda pair: (
            pair[1].get('progress_pct') is None,
            -float(pair[1].get('progress_pct') or 0),
            pair[0],
        ),
    )
    lines.append(f'【持仓】（{len(positions)}只，有信号履历的按信号推进降序，系统外持仓垫底）：')
    lines.extend([render_position(position) for _, position in ordered])
    index_points = render_index_points(market)
    if index_points:
        lines.append(index_points)
    lines.extend(['【明日计划】：', render_plan(market, market.get('t_symbols') or ())])
    closing = render_closing(market, market.get('receipt_coverage'))
    if closing:
        lines.append(closing)
    lines.append('个人观点，仅供参考。')
    return '\n'.join(lines)
