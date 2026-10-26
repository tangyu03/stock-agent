from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from ..core.calendar import trading_days_between
from ..core.selector import COOLDOWN_STOP_COUNT, MA_FAMILY
from ..models.context_mark import ContextMark
from ..models.event_calendar import EventCalendar
from ..models.signal import Signal, SignalRole, SignalStatus

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


def render_execution_review(rows: Iterable[Mapping[str, Any]], names: Mapping[str, str] | None = None) -> str:
    items = list(rows)
    if not items:
        return '【今日执行回顾】：今日无触发信号。'
    lines = ['【今日执行回顾】：']
    for row in items:
        verdict_map = {'executed': '已执行', 'ignored': '未执行', 'veto': '否决'}
        verdict = verdict_map.get(_text(row.get('verdict'))) or '未回执'
        difference = {'ignored': '机械信号未执行', 'veto': '人工否决机械信号'}.get(_text(row.get('difference')), _text(row.get('difference')))
        suffix = f'，差异：{difference}' if difference else ''
        symbol = _text(row.get('symbol'))
        strategy_id = _text(row.get('strategy_id'))
        tag = _text(row.get('tag'))
        name = (names or {}).get(symbol) or _text(row.get('name')) or symbol
        lines.append(f"{name}（{symbol}） {_strategy_label(strategy_id)} {_TAG_LABELS.get(tag, tag)} {_price(row.get('zone_low'))}–{_price(row.get('zone_high'))} 回执：{verdict}{suffix}")
    return '\n'.join(lines)


_TAG_LABELS = {'enter': '买入', 'stop': '止损', 'profit': '止盈', 't': '做T', 'retest_confirm': '回踩确认'}


def render_ev_negative(market: Mapping[str, Any]) -> str:
    strategies = list(market.get('ev_negative_strategies') or [])
    if not strategies:
        return ''
    return '【EV_NEG 记录】：' + '、'.join(_strategy_label(str(item)) for item in strategies) + '（仅记录，不推送预警）。'


def render_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    signal_rows: list[Signal] | None = None,
) -> str:
    if signal_rows is not None:
        return render_signal_report(report_date, market, signal_rows)
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


def _signal_name(signal: Signal, names: Mapping[str, str]) -> str:
    return names.get(signal.symbol) or signal.symbol


_STRATEGY_LABELS = {
    'holding': '持仓保护',
    'first_move_5d': '5日线首动',
    'first_move_10d': '10日线首动',
    'first_move_drift': '漂移突破',
    'first_board_dip': '首板回踩',
    'volume_breakout': '放量突破',
}

_BREAK_QUALIFIER_LABELS = {
    'DELAY_OBSERVE': '延迟观察',
    'EXECUTE_NOW': '立即执行',
    'DIVERGENCE': '量价背离',
    'LIMIT_EXEMPT': '涨停豁免',
}

_STATUS_REASON_LABELS = {
    'active_volume_break': '放量主动卖压',
    'passive_or_low_volume_break': '被动破位或缩量',
    'limit_up_price_distortion': '涨停价格失真',
    'divergence_needs_close': '量价背离需收盘确认',
    'retest_pass': '回踩确认',
    'retest_fail': '回踩失败',
    'NOT_CLASSIFIED': '未分类',
}

_ROLE_LABELS = {
    'entry': '买入',
    'stop': '止损',
    'profit': '止盈',
    't': '做T',
    'retest_confirm': '回踩确认',
}

_CONTEXT_TYPE_LABELS = {'index': '指数', 'sector': '板块'}


def _strategy_label(strategy_id: str) -> str:
    return _STRATEGY_LABELS.get(strategy_id, strategy_id)


def _signal_line(signal: Signal, names: Mapping[str, str]) -> str:
    window = ''
    if signal.trigger_window_start and signal.trigger_window_end:
        window = f"｜触发窗口 {signal.trigger_window_start.strftime('%H:%M')}-{signal.trigger_window_end.strftime('%H:%M')}"
    qualifier_text = _BREAK_QUALIFIER_LABELS.get(signal.break_qualifier or '', signal.break_qualifier or '')
    qualifier = f"｜{qualifier_text}" if qualifier_text else ''
    role_text = _ROLE_LABELS.get(signal.signal_role.value, signal.signal_role.value)
    role_prefix = f"【{role_text}】" if signal.signal_role is not SignalRole.ENTRY else ''
    line = (
        f"{_signal_name(signal, names)}（{signal.symbol}）{role_prefix} {_strategy_label(signal.strategy_id)} "
        f"{_price(signal.zone_low)}–{_price(signal.zone_high)}"
        f"{window}{qualifier}"
    )
    premise = _text(signal.premise.get('premise'))
    if premise:
        line += f"｜前提 {premise}"
    if signal.valid_policy.value == 'next_day':
        line += '｜次日有效'
    if signal.activation == 'trailing':
        line += '｜移动止盈'
    return line


def _signal_line_with_market(signal: Signal, market: Mapping[str, Any], names: Mapping[str, str]) -> str:
    line = _signal_line(signal, names)
    assignments = {
        str(row.get('symbol') or ''): row
        for row in (market.get('assignments') or [])
    }
    price = assignments.get(signal.symbol, {}).get('est_close')
    if price is not None:
        line += f"｜现价 {_price(float(price))}"
    assignment = assignments.get(signal.symbol, {})
    details = []
    if assignment.get('support') is not None and assignment.get('resistance') is not None:
        details.append(f"支撑/压力 {_price(assignment['support'])}/{_price(assignment['resistance'])}")
    if assignment.get('breakout_price') is not None:
        details.append(f"突破位 {_price(assignment['breakout_price'])}")
    if assignment.get('two_source_anomaly'):
        details.append('两源异常，统计剔除')
    if details:
        line += '｜' + '，'.join(details)
    if signal.size_hint is not None and str(market.get('tier') or '').upper() == 'LOW':
        line += f"｜LOW档仓位 {signal.size_hint}"
    return line


def _assignment(signal: Signal, market: Mapping[str, Any]) -> Mapping[str, Any]:
    return next(
        (
            row for row in (market.get('assignments') or [])
            if str(row.get('symbol') or '') == signal.symbol
        ),
        {},
    )


def _retest_window_days(market: Mapping[str, Any]) -> int:
    try:
        return max(0, int(market.get('retest_window_days') or 3))
    except (TypeError, ValueError):
        return 3


def _retest_holidays(market: Mapping[str, Any]) -> set[date]:
    holidays: set[date] = set()
    for value in market.get('holidays') or []:
        try:
            holidays.add(date.fromisoformat(str(value)))
        except (TypeError, ValueError):
            continue
    return holidays


def _retest_elapsed_days(signal: Signal, report_date: str, market: Mapping[str, Any]) -> int:
    try:
        end = date.fromisoformat(report_date)
    except (TypeError, ValueError):
        return 0
    return trading_days_between(signal.created_at.date(), end, _retest_holidays(market))


def _retest_observation_note(signal: Signal, report_date: str, market: Mapping[str, Any]) -> str:
    assignment = _assignment(signal, market)
    close = assignment.get('est_close')
    high = assignment.get('high')
    touched = (
        high is not None
        and close is not None
        and float(high) >= float(signal.zone_low)
        and float(close) < float(signal.zone_high)
    )
    if touched:
        note = (
            f"今日最高{_price(high)}摸到区间，收{_price(close)}未站上。"
            "明日收盘仍在"
            f"{_price(signal.zone_high)}下方→确认破位。"
        )
    else:
        note = '未回抽。'
    remaining = max(0, _retest_window_days(market) - _retest_elapsed_days(signal, report_date, market))
    return f"{note}剩{remaining}日。（观察期内不出买点）"


def _retest_judgment_note(signal: Signal, report_date: str, market: Mapping[str, Any]) -> str:
    assignment = _assignment(signal, market)
    close = assignment.get('est_close')
    high = assignment.get('high')
    window = _retest_window_days(market)
    if signal.status is SignalStatus.TRIGGERED:
        detail = f"（最高{_price(high)}）" if high is not None else ''
        return f"{window}日反抽未收复{_price(signal.zone_high)}{detail}，原支撑确认转为压力。观察哨关闭。"
    if signal.status is SignalStatus.INVALIDATED:
        volume = assignment.get('volume_ratio')
        volume_text = f"，量比{_price(volume)}（达标）" if volume is not None else ''
        close_text = f"收盘{_price(close)}" if close is not None else ''
        return f"{close_text}收复观察区间上沿{_price(signal.zone_high)}{volume_text}，原止损逻辑失效。观察哨关闭。"
    return f"{window}日窗口到期，价格未回抽{_price(signal.zone_low)}–{_price(signal.zone_high)}。观察哨关闭。"


def _cooldown_note(signal: Signal, market: Mapping[str, Any]) -> str:
    counts = (market.get('recent_stop_counts') or {}).get(signal.symbol) or {}
    current_family = MA_FAMILY.get(signal.strategy_id, signal.strategy_id)
    notes: list[str] = []
    for family, hits in counts.items():
        try:
            cooled = int(hits or 0) >= COOLDOWN_STOP_COUNT
        except (TypeError, ValueError):
            cooled = False
        if not cooled:
            continue
        family_text = '5日线' if family == 'ma5' else '10日线' if family == 'ma10' else str(family)
        notes.append(f'{family_text}档位冷却中')
        if family == current_family:
            break
    return '｜' + '，'.join(notes) if notes else ''


def _render_context_marks(marks: Iterable[ContextMark], names: Mapping[str, str]) -> str:
    rows = list(marks)
    if not rows:
        return ''
    lines = ['【环境】：']
    for mark in rows:
        context_type = _CONTEXT_TYPE_LABELS.get(mark.context_type.value, mark.context_type.value)
        mark_name = names.get(mark.code) or ('上证指数' if mark.code == 'sh000001' else mark.code)
        triggered = f"｜最近触发 {mark.triggered_at:%H:%M}" if mark.triggered_at else ''
        lines.append(
            f"{context_type} {mark_name}（{mark.code}） {_price(mark.level_low)}–{_price(mark.level_high)}"
            f" {mark.label}{triggered}"
        )
    return '\n'.join(lines)


def _render_event_calendar(events: Iterable[EventCalendar], names: Mapping[str, str]) -> str:
    rows = list(events)
    if not rows:
        return ''
    lines = ['【事件】：']
    for event in rows:
        event_name = names.get(event.code) or event.code
        strategy_id = str((event.payload or {}).get('strategy_id') or '')
        label = f'｜{_strategy_label(strategy_id)}' if strategy_id else ''
        lines.append(f"{event.due_time:%H:%M} {event_name}（{event.code}）{label}")
    return '\n'.join(lines)


def render_signal_report(
    report_date: str,
    market: Mapping[str, Any],
    signals: list[Signal],
) -> str:
    names = {str(code): str(name) for code, name in (market.get('name_map') or {}).items()}
    scenario_date = str(market.get('signal_date') or report_date)
    entries = [signal for signal in signals if signal.signal_role is SignalRole.ENTRY and signal.status is SignalStatus.ACTIVE]
    active_protected = [
        signal for signal in signals
        if signal.signal_role in {SignalRole.STOP, SignalRole.PROFIT, SignalRole.T}
        and signal.status is SignalStatus.ACTIVE
    ]
    protected_by_key: dict[tuple[str, SignalRole], Signal] = {}
    for signal in active_protected:
        key = (signal.symbol, signal.signal_role)
        existing = protected_by_key.get(key)
        if existing is None or signal.created_at > existing.created_at:
            protected_by_key[key] = signal
    protected = list(protected_by_key.values())
    breaks = [
        signal for signal in signals
        if signal.signal_role is SignalRole.STOP
        and signal.status is SignalStatus.TRIGGERED
        and signal.closed_at and scenario_date <= signal.closed_at.date().isoformat() <= report_date
    ]
    observations = [signal for signal in signals if signal.signal_role is SignalRole.RETEST_CONFIRM and signal.status is SignalStatus.ACTIVE]
    judgments = [
        signal for signal in signals
        if signal.signal_role is SignalRole.RETEST_CONFIRM
        and signal.status in {SignalStatus.TRIGGERED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
        and signal.closed_at and scenario_date <= signal.closed_at.date().isoformat() <= report_date
    ]
    lines = [f'【{report_date} 交易计划】', render_header(market)]
    context_marks = [ContextMark.model_validate(row) for row in (market.get('context_marks') or [])]
    context_text = _render_context_marks(context_marks, names)
    if context_text:
        lines.append(context_text)
    event_text = _render_event_calendar(market.get('event_calendar') or [], names)
    if event_text:
        lines.append(event_text)
    execution_review = list(market.get('execution_review') or [])
    if execution_review:
        lines.append(render_execution_review(execution_review, names))
    ev_negative = render_ev_negative(market)
    if ev_negative:
        lines.append(ev_negative)
    if bool(market.get('zero_plan_day')):
        lines.append('【明日零预案】：不开新仓；破位定性取消延迟判决。')
    candidate_note = '不合并同票多策略'
    if (
        market.get('watchlist_limit') is not None
        and market.get('pool_total') is not None
        and int(market['watchlist_limit']) < int(market['pool_total'])
    ):
        candidate_note += f"，池子 {market['pool_total']}→{market['watchlist_limit']}"
    lines.append(f'【候选】（{len(entries)} 条，{candidate_note}）：')
    entry_lines = [
        _signal_line_with_market(signal, market, names) + _cooldown_note(signal, market)
        for signal in entries
    ]
    lines.extend(entry_lines or ['无'])
    protected_symbols = {signal.symbol for signal in observations}
    shown_protection = [signal for signal in protected if signal.symbol not in protected_symbols]
    lines.append(f'【池内票】（{len(shown_protection)} 条保护行）：')
    lines.extend([_signal_line_with_market(signal, market, names) for signal in shown_protection] or ['无'])
    lines.append(f'【破位】（{len(breaks)} 条）：')
    lines.extend([_signal_line_with_market(signal, market, names) for signal in breaks] or ['无'])
    lines.append(f'【观察区】（{len(observations)} 条）：')
    lines.extend([
        _signal_line_with_market(signal, market, names) + '｜' + _retest_observation_note(signal, report_date, market)
        for signal in observations
    ] or ['无'])
    lines.append(f'【判决区】（{len(judgments)} 条）：')
    judgment_status = {
        'TRIGGERED': '确认',
        'EXPIRED': '窗口失效',
        'INVALIDATED': '前提失效',
    }
    judgment_lines = []
    for signal in judgments:
        line = _signal_line_with_market(signal, market, names)
        try:
            observed_days = max(0, (date.fromisoformat(report_date) - signal.created_at.date()).days)
        except (TypeError, ValueError):
            observed_days = 0
        line += f"｜{judgment_status.get(signal.status.value, signal.status.value)}｜已观察{observed_days}天"
        if signal.status_reason:
            reason = _STATUS_REASON_LABELS.get(signal.status_reason, signal.status_reason)
            line += f"｜原因 {reason}"
        line += '｜' + _retest_judgment_note(signal, report_date, market)
        judgment_lines.append(line)
    lines.extend(judgment_lines or ['无'])
    closing = render_closing(market, market.get('receipt_coverage'))
    if closing:
        lines.append(closing)
    lines.append('个人观点，仅供参考。')
    return '\n'.join(lines)
