from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from ..core.calendar import trading_days_between
from ..core.selector import COOLDOWN_STOP_COUNT, MA_FAMILY
from ..models.context_mark import ContextMark
from ..models.event_calendar import EventCalendar
from ..models.signal import Signal, SignalRole, SignalSide, SignalStatus

def _text(value: Any) -> str:
    return str(value if value is not None else '').strip()


def _price(value: Any) -> str:
    try:
        return f'{float(value):g}'
    except (TypeError, ValueError):
        return ''


_INDEX_NAMES = {
    'sh000001': '上证指数',
    'sz399001': '深证成指',
    'sh000688': '科创50',
}


def render_header(market: Mapping[str, Any], report_date: str | None = None) -> str:
    amount = market.get('amount_yi')
    tier = str(market.get('tier', '')).upper()
    parts: list[str] = []
    if amount is not None and tier == 'LOW':
        parts.append(f'量能 {_price(amount)}亿，架构不起行情，只架得起轮动')
    elif amount is not None and tier == 'HIGH':
        parts.append(f'久违的{_price(amount)}亿。')
    summary = list(market.get('index_summary') or [])
    if not summary and market.get('sh_close') is not None:
        summary = [{
            'symbol': 'sh000001',
            'close': market.get('sh_close'),
            'ma5': market.get('sh_ma5'),
        }]
    index_parts = []
    for row in summary:
        symbol = _text(row.get('symbol') or 'sh000001')
        close = float(row.get('close') or 0)
        ma5 = float(row.get('ma5') or 0)
        if not close or not ma5:
            continue
        position = '上' if close >= ma5 else '下'
        distance = abs(close - ma5) / ma5 * 100
        index_parts.append(
            f"{_INDEX_NAMES.get(symbol, symbol)} {close:g}/MA5 {ma5:g}（{position}{distance:.2f}%）"
        )
    if index_parts:
        parts.append('指数 ' + '、'.join(index_parts))
    sector_parts = []
    for mark in market.get('context_marks') or []:
        if str(mark.get('context_type') or '') != 'sector':
            continue
        label = _text(mark.get('label')) or _text(mark.get('code'))
        zone = f"{_price(mark.get('level_low'))}–{_price(mark.get('level_high'))}"
        suffix = '（板块数据过期）' if str(mark.get('data_status') or 'OK').upper() == 'STALE' else ''
        sector_parts.append(f'{label}{suffix} {zone}')
    if sector_parts:
        parts.append('板块 ' + '；'.join(sector_parts))
    if market.get('sector_down') and bool(market.get('pool_reversal')):
        parts.append('资金总要找个宣泄口')
    facts: list[str] = []
    base_date = _text(market.get('signal_date') or market.get('trade_date'))
    valid_date = _text(report_date or market.get('report_date') or market.get('valid_for'))
    if base_date and valid_date:
        facts.append(f'数据基准日：{base_date}｜计划有效日：{valid_date}')
    if base_date:
        facts.append(f"报告状态：{_text(market.get('report_status') or '已结算')}")
        facts.append(f"熔断日：{'是' if market.get('circuit_day') else '否'}")
    prefix = '\n'.join(facts)
    return prefix + ('\n' if prefix and parts else '') + '。'.join(parts) + ('。' if parts else '')


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
    if position.get('missed_stop_date'):
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
    lines.append('4）先手点位已出，尾盘见。')
    return '\n'.join(lines)


def render_closing(market: Mapping[str, Any]) -> str:
    if bool(market.get('missed_stop_hit')):
        return '亏了别去找逻辑安慰自己。逻辑是用来辅助上涨的，不是用来强化扛单信念的。市场永远是对的。'
    if str(market.get('tier', '')).upper() == 'LOW':
        return '少动就是赢。'
    return ''


def render_trigger_review(rows: Iterable[Mapping[str, Any]], names: Mapping[str, str] | None = None) -> str:
    items = list(rows)
    if not items:
        return '无'
    lines = []
    for row in items:
        signal_id = _text(row.get('signal_id'))
        symbol = _text(row.get('symbol'))
        strategy_id = _text(row.get('strategy_id'))
        tag = _text(row.get('tag'))
        name = (names or {}).get(symbol) or _text(row.get('name')) or symbol
        kind = _TAG_LABELS.get(tag, tag)
        side = _text(row.get('side'))
        strategy = _strategy_label(strategy_id)
        if tag == 't' and side:
            kind = f'{kind} {side}'
        kind_text = f'{kind}（对应 {strategy}）' if strategy else kind
        metadata = f"｜signal_id={signal_id}" if signal_id else ''
        pair_id = _text(row.get('pair_id'))
        if pair_id:
            metadata += '｜pair_id=' + pair_id
        triggered_at = _text(row.get('triggered_at'))
        if triggered_at:
            metadata += f"｜触发={triggered_at}"
        created_at = _text(row.get('created_at'))
        if created_at:
            metadata += f"｜创建={created_at}"
        lines.append(
            f"触发：{name}（{symbol}） {kind_text} "
            f"{_price(row.get('zone_low'))}—{_price(row.get('zone_high'))}"
            f"{metadata}"
        )
    return '\n'.join(lines)


_TAG_LABELS = {'enter': '买入', 'stop': '止损', 'profit': '止盈', 't': '做T', 'retest_confirm': '回踩确认'}


def render_ev_negative(market: Mapping[str, Any]) -> str:
    strategies = [
        strategy for strategy in (market.get('ev_negative_strategies') or [])
        if strategy not in {'pool', 'holding'}
    ]
    if not strategies:
        return ''
    return '【EV_NEG 记录】：' + '、'.join(_strategy_label(str(item)) for item in strategies) + '（仅记录，不推送预警）。'


def _render_break_with_settlement(
    signal: Signal,
    names: Mapping[str, str],
    settlement: Mapping[str, Any] | None,
    market: Mapping[str, Any],
) -> str:
    raw_qualifier = _text((settlement or {}).get('raw_qualifier')) or signal.break_qualifier
    effective_qualifier = _text((settlement or {}).get('effective_qualifier'))
    outcome = _text((settlement or {}).get('settlement_outcome')) or signal.settlement_outcome
    line = (
        f"{_signal_name(signal, names)} stop {_price(signal.zone_low)}–{_price(signal.zone_high)}"
        f"（原始定性：{_BREAK_QUALIFIER_LABELS.get(raw_qualifier, raw_qualifier or '未分类')}"
    )
    if effective_qualifier:
        line += (
            "｜有效定性："
            + _BREAK_QUALIFIER_LABELS.get(effective_qualifier, effective_qualifier)
        )
    if settlement is not None:
        circuit = bool(settlement.get('circuit_day'))
        line += '｜熔断日：是' if circuit else '｜熔断日：否'
        close_price = settlement.get('close_price')
        if close_price is not None:
            line += '｜收盘价 ' + _price(close_price)
    if outcome:
        line += '｜收盘结果：' + _SETTLEMENT_OUTCOME_LABELS.get(outcome, outcome)
    line += "）" + chr(10) + '  ' + _break_settlement_note(signal, settlement)
    return line


def render_report(
    report_date: str,
    market: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    signal_rows: list[Signal] | None = None,
) -> str:
    if signal_rows is not None:
        return render_signal_report(
            report_date, market, signal_rows,
            stop_settlements=list(market.get('stop_settlements') or []),
        )
    lines = [f'【{report_date} 复盘】', render_header(market)]
    ev_negative = render_ev_negative(market)
    if ev_negative:
        lines.append(ev_negative)
    trigger_review = list(market.get('execution_review') or [])
    if trigger_review:
        lines.append(render_trigger_review(trigger_review))
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
    closing = render_closing(market)
    if closing:
        lines.append(closing)
    lines.append('个人观点，仅供参考。')
    return '\n'.join(lines)


def _signal_name(signal: Signal, names: Mapping[str, str]) -> str:
    return names.get(signal.symbol) or signal.symbol


_STRATEGY_LABELS = {
    'holding': '池内',
    'pool': '池内',
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
    'SAME_DAY_RECOVERED': '收盘收复（历史字段）',
    'UNCLASSIFIED': '定性待补',
}

_SETTLEMENT_OUTCOME_LABELS = {
    'EXECUTE_NOW': '当日执行',
    'STILL_BROKEN': '收盘仍破',
    'WEAK_MARKET_EXECUTE': '弱市执行',
    'SAME_DAY_RECOVERED': '收复止损区下沿',
    'PENDING_DATA': '待补数据',
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

_INVALIDATION_LABELS = {
    'first_move_5d': '（区间下沿，明日MA5×0.99）',
    'first_move_10d': '（区间下沿，明日MA10×0.99）',
    'first_move_drift': '（区间下沿，明日MA5×0.99）',
    'first_board_dip': '（MA5）',
    'volume_breakout': '（突破位）',
}

_RATIONALE_LABELS = {
    'first_move_5d': '5日线买在明天的位置，破5日就是卖点',
    'first_move_10d': '10日线买在明天的位置，破10日就是卖点',
    'first_move_drift': '5日线正在下移，尾盘突破按反转尝试处理',
    'first_board_dip': '首板后回踩分时支撑，破5日线离场',
    'volume_breakout': '放量突破关键位后买突破；收盘跌回突破位就是失效',
}

_T_SUPPRESSION_LABELS = {
    'same_day_triggered_stop': '当日已有止损触发，暂停T行',
    't_buy_overlaps_stop': 'T买与止损区间重叠，暂停T行',
    'invalid_t_zone': 'T区间无效，暂停T行',
}


def _strategy_label(strategy_id: str) -> str:
    return _STRATEGY_LABELS.get(strategy_id, strategy_id)


def _signal_line(signal: Signal, names: Mapping[str, str]) -> str:
    window = ''
    if signal.trigger_window_start and signal.trigger_window_end:
        window = f"｜触发窗口 {signal.trigger_window_start.strftime('%H:%M')}-{signal.trigger_window_end.strftime('%H:%M')}"
    qualifier_text = _BREAK_QUALIFIER_LABELS.get(signal.break_qualifier or '', signal.break_qualifier or '')
    qualifier = f"｜{qualifier_text}" if qualifier_text else ''
    role_text = _ROLE_LABELS.get(signal.signal_role.value, signal.signal_role.value)
    if signal.signal_role is SignalRole.T:
        role_text = f'做T {signal.side.value}'
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
    elif signal.valid_for:
        line += '｜有效 ' + signal.valid_for.isoformat()
    if signal.activation == 'trailing':
        line += '｜移动止盈'
    return line


def _signal_line_with_market(signal: Signal, market: Mapping[str, Any], names: Mapping[str, str]) -> str:
    line = _signal_line(signal, names)
    assignments = {
        str(row.get('symbol') or ''): row
        for row in (market.get('assignments') or [])
    }
    spot_prices = {
        str(code): value
        for code, value in (market.get('spot_prices') or {}).items()
    }
    price = spot_prices.get(signal.symbol)
    if price is None:
        price = assignments.get(signal.symbol, {}).get('est_close')
    if price is not None:
        line += f"｜现价 {_price(float(price))}"
    assignment = assignments.get(signal.symbol, {})
    details = []
    if assignment.get('two_source_anomaly'):
        details.append('两源异常，统计剔除')
    if details:
        line += '｜' + '，'.join(details)
    if signal.size_hint is not None and str(market.get('tier') or '').upper() == 'LOW':
        line += f"｜LOW档仓位 {signal.size_hint}"
    return line


def _entry_invalidation(signal: Signal, market: Mapping[str, Any]) -> str:
    invalidation = signal.premise.get('invalidation_price')
    if invalidation is None:
        assignment = _assignment(signal, market)
        if signal.strategy_id == 'volume_breakout':
            invalidation = assignment.get('breakout_price')
        if invalidation is None:
            invalidation = float(signal.zone_low)
    note = str(signal.premise.get('invalidation_note') or _INVALIDATION_LABELS.get(signal.strategy_id, '（失效线）'))
    if note.startswith('收盘破'):
        note = note[len('收盘破'):]
    if not note.startswith('（'):
        note = f'（{note}）'
    return f"收盘破{_price(float(invalidation))}{note}"


def _entry_rationale(signal: Signal) -> str:
    return str(signal.premise.get('rationale') or _RATIONALE_LABELS.get(signal.strategy_id, ''))


def _signal_spot_line(signal: Signal, market: Mapping[str, Any]) -> str:
    assignment = _assignment(signal, market)
    spot_prices = {
        str(code): value
        for code, value in (market.get('spot_prices') or {}).items()
    }
    price = spot_prices.get(signal.symbol)
    if price is None:
        price = assignment.get('est_close')
    if price is None:
        return ''
    return f"    现价：{_price(float(price))}"


def _entry_reachability_note(signal: Signal, market: Mapping[str, Any]) -> str:
    assignment = _assignment(signal, market)
    close = assignment.get('est_close')
    if close is None:
        return ''
    zone_low = float(signal.zone_low)
    zone_high = float(signal.zone_high)
    close_value = float(close)
    if close_value > zone_high:
        position = '高于买入区间'
    elif close_value < zone_low:
        position = '低于买入区间'
    else:
        position = '落入买入区间'
    return f'    基准日参考：收盘{close_value:.3g}，{position}，待有效日评估'


def _candidate_audit_note(market: Mapping[str, Any], valid_count: int) -> str:
    reason_labels = {
        'active_retest_confirm': '观察哨存续',
        'missing_kline': '缺K线',
        'stale_kline': 'K线过期',
        'insufficient_closes': '均线数据不足',
        'no_strategy': '前提不满足',
        'lhb_data_unavailable': '龙虎榜数据不可用',
    }
    counts: dict[str, int] = {}
    for row in (market.get('rejected') or []):
        reason = str(row.get('reason') or 'unknown') if isinstance(row, Mapping) else 'unknown'
        counts[reason] = counts.get(reason, 0) + 1
    generated = market.get('pool_total')
    if generated is None:
        generated = valid_count + sum(counts.values())
    reason_text = '｜'.join(
        f'{reason_labels.get(reason, reason)}{count}'
        for reason, count in sorted(counts.items())
    ) or '无排除'
    return f'候选对账：生成{generated}｜{reason_text}｜有效候选{valid_count}'


def _break_settlement_note(signal: Signal, settlement: Mapping[str, Any] | None) -> str:
    outcome = _text((settlement or {}).get('settlement_outcome'))
    close = (settlement or {}).get('close_price')
    close_text = f'收盘{_price(close)}' if close is not None else '收盘价缺失'
    data_status = _text((settlement or {}).get('data_status') or 'OK')
    if outcome == 'PENDING_DATA':
        return f'待补数据（{data_status}）：已触发 stop，不默认定性，不生成后续动作。'
    if outcome == 'EXECUTE_NOW':
        return f'{close_text}，按落库事实当日执行，不立观察哨。'
    if outcome == 'RECOVERED_CORRECTION':
        return f'{close_text}收复止损区下沿{_price(signal.zone_low)}，当日假破位，纠错提醒。'
    if outcome == 'CREATE_RETEST':
        return f'{close_text}仍破{_price(signal.zone_low)}，生成反抽观察哨。'
    if outcome == 'LIMIT_EXEMPT':
        return '涨停价豁免，不产生观察哨。'
    return '结算事实缺失，报告待补数据。'


def _merge_correction_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row['trade_date']), str(row['symbol']))
        if key not in grouped:
            grouped[key] = {**dict(row), 'strategy_ids': [], 'notices': []}
        bucket = grouped[key]
        strategy_id = str(row.get('strategy_id') or '')
        if strategy_id and strategy_id not in bucket['strategy_ids']:
            bucket['strategy_ids'].append(strategy_id)
        notice = str(row.get('notice') or '')
        if notice and notice not in bucket['notices']:
            bucket['notices'].append(notice)
        if bucket.get('trigger') and bucket['trigger'] != row.get('trigger'):
            bucket['trigger'] = 'mixed'
    return [
        {**row, 'notice': '；'.join(row.pop('notices'))}
        for row in grouped.values()
    ]


def _render_candidate_groups(
    entries: list[Signal],
    pool_signals: list[Signal],
    market: Mapping[str, Any],
    names: Mapping[str, str],
) -> list[str]:
    by_symbol: dict[str, list[Signal]] = {}
    for signal in entries:
        by_symbol.setdefault(signal.symbol, []).append(signal)
    lines: list[str] = []
    for symbol, rows in by_symbol.items():
        lines.append(f'{_signal_name(rows[0], names)}（{symbol}）')
        for signal in rows:
            lines.append(
                f"  entry  {_strategy_label(signal.strategy_id)}  "
                f"{_price(signal.zone_low)}–{_price(signal.zone_high)}"
            )
            window = (
                f"{signal.trigger_window_start:%H:%M}–{signal.trigger_window_end:%H:%M}"
                if signal.trigger_window_start and signal.trigger_window_end
                else '全天有效'
            )
            lines.append(f'    触发窗口：{window}')
            premise = _text(signal.premise.get('premise'))
            if premise:
                lines.append(f'    前提：{premise}')
            lines.append(f'    失效：{_entry_invalidation(signal, market)}')
            rationale = _entry_rationale(signal)
            if rationale:
                lines.append(f'    依据：{rationale}')
            cooldown = _cooldown_note(signal, market)
            if cooldown:
                lines.append(f'    均线冷却：{cooldown.lstrip("｜")}')
            spot = _signal_spot_line(signal, market)
            if spot:
                lines.append(spot)
            reachability = _entry_reachability_note(signal, market)
            if reachability:
                lines.append(reachability)
            overlap_stop = next(
                (
                    row for row in pool_signals
                    if row.symbol == symbol
                    and row.signal_role is SignalRole.STOP
                    and row.zone_low <= signal.zone_high
                    and row.zone_high >= signal.zone_low
                ),
                None,
            )
            if overlap_stop is not None:
                lines.append('    提示：候选entry与池内stop区间重合；空仓看买点，持仓看风控。')
            if signal.size_hint is not None and str(market.get('tier') or '').upper() == 'LOW':
                lines.append('    仓位提示：减半（量能LOW）')
    return lines


_PROTECTION_REASON_LABELS = {
    'retest_active': '回踩观察哨存续',
    'retest_confirmed': '回踩确认破位',
    'retest_expired': '回踩窗口到期',
    'execute_now': '破位当日执行',
    'reentered': '新enter已触发',
    'limit_exempt': '涨停价豁免',
}


def _render_pool_groups(
    signals: list[Signal],
    entries: list[Signal],
    market: Mapping[str, Any],
    names: Mapping[str, str],
    report_date: str,
    observations: list[Signal],
    protection_states: Mapping[str, tuple[str, str]] | None = None,
) -> list[str]:
    by_symbol: dict[str, list[Signal]] = {}
    for signal in signals:
        by_symbol.setdefault(signal.symbol, []).append(signal)
    entry_by_id = {
        signal.id: signal
        for signal in signals
        if signal.tag.value == 'enter'
    }
    lines: list[str] = []
    for symbol, rows in by_symbol.items():
        trend = rows[0].trend.value
        lines.append(f'{_signal_name(rows[0], names)}（{symbol}｜{trend}）')
        ordered = {
            SignalRole.STOP: None,
            SignalRole.PROFIT: None,
            SignalRole.T: [],
        }
        for signal in rows:
            if signal.signal_role is SignalRole.T:
                ordered[SignalRole.T].append(signal)
            else:
                ordered[signal.signal_role] = signal
        parts = []
        if ordered[SignalRole.STOP]:
            signal = ordered[SignalRole.STOP]
            parts.append(f"stop {_price(signal.zone_low)}–{_price(signal.zone_high)}")
        if ordered[SignalRole.PROFIT]:
            signal = ordered[SignalRole.PROFIT]
            parts.append(f"止盈 {_price(signal.zone_low)}–{_price(signal.zone_high)}")
        for signal in ordered[SignalRole.T]:
            side = 't买' if signal.side is SignalSide.BUY else 't卖'
            parts.append(f"{side} {_price(signal.zone_low)}–{_price(signal.zone_high)}")
        if parts:
            lines.append('  点位：' + '｜'.join(parts))
        spot = _signal_spot_line(rows[0], market)
        if spot:
            lines.append(spot)
        parent_id = rows[0].parent_id
        parent = next(
            (
                signal for signal in [*signals, *entries]
                if signal.id == str(parent_id) and signal.tag.value == 'enter'
            ),
            None,
        )
        if parent:
            lines.append(f"  parent={parent_id}｜策略={_strategy_label(parent.strategy_id)}")
        else:
            lines.append(f'  parent={parent_id}')
        if trend == 'DOWN':
            if ordered[SignalRole.STOP]:
                lines.append('  提示：趋势DOWN，不生成T行、BUY行；现有stop按卖出保护处理。')
            else:
                lines.append('  提示：趋势DOWN，保护行待重建／等待新enter。无T行、无BUY行。')
        elif trend == 'UP' and not ordered[SignalRole.T]:
            reason = next((
                signal.premise.get('t_suppression_reason')
                for signal in rows if signal.premise.get('t_suppression_reason')
            ), None)
            reason_text = _T_SUPPRESSION_LABELS.get(str(reason), 'T行生成条件未满足')
            lines.append(f'  提示：{reason_text}。')
    for signal in observations:
        remaining = max(0, _retest_window_days(market) - _retest_elapsed_days(signal, report_date, market))
        lines.append(f"{_signal_name(signal, names)}（观察哨存续期）")
        lines.append(
            f"  提示：观察哨存续（{_price(signal.zone_low)}–{_price(signal.zone_high)}，剩{remaining}日），"
            "保护语义由观察哨承担，本票暂不生成 stop/profit/t"
        )
    # §8 保护状态：信号缺失的票也要显示结构化原因
    protection_mode_labels = {
        'BLOCKED': '回踩观察中，暂停普通 stop/profit/t 与普通 enter',
        'WAIT_REENTRY': '等待新 enter，保护语义挂起',
    }
    shown_symbols = {signal.symbol for signal in signals} | {signal.symbol for signal in observations}
    for symbol, (mode, reason) in (protection_states or {}).items():
        if mode == 'NORMAL' or symbol in shown_symbols:
            continue
        mode_text = protection_mode_labels.get(mode, mode)
        reason_text = _PROTECTION_REASON_LABELS.get(reason, reason or '未标注')
        lines.append(f'{names.get(symbol, symbol)}（{symbol}）保护状态：{mode_text}（{reason_text}）')
    return lines


def _assignment(signal: Signal, market: Mapping[str, Any]) -> Mapping[str, Any]:
    assignment = next(
        (
            row for row in (market.get('assignments') or [])
            if str(row.get('symbol') or '') == signal.symbol
        ),
        {},
    )
    if assignment:
        return assignment
    return next(
        (
            row for row in (market.get('signal_rows') or [])
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


def _retest_observation_line(signal: Signal, report_date: str, market: Mapping[str, Any], names: Mapping[str, str]) -> str:
    qualifier = _BREAK_QUALIFIER_LABELS.get(signal.break_qualifier or '', signal.break_qualifier or '未分类')
    break_date = signal.created_at.date().isoformat()[5:].replace('-', '.')
    note = _retest_observation_note(signal, report_date, market)
    return (
        f'{_signal_name(signal, names)}（{signal.symbol}）：破位日{break_date}（{qualifier}），'
        f"反抽观察{_price(signal.zone_low)}–{_price(signal.zone_high)}，{note}"
    )


def _retest_judgment_note_with_market(signal: Signal, report_date: str, market: Mapping[str, Any]) -> str:
    '''§7.5 判决区：优先渲染落库 verdict_facts（观察窗口/期间最高/入区事实/收盘与上沿），
    旧数据无 verdict_facts 时回退到状态+观察天数。'''
    facts = signal.premise.get('verdict_facts') or {}
    if facts.get('outcome'):
        window_text = (
            f"{_text(facts.get('window_start'))}至{_text(facts.get('window_end'))}"
            if facts.get('window_start') and facts.get('window_end')
            else f"已观察{_price(facts.get('observed_days')) or '?'}交易日"
        )
        high_text = f"，期间最高{_price(facts.get('max_high'))}" if facts.get('max_high') is not None else ''
        if facts['outcome'] == 'TRIGGERED':
            close_text = (
                f"，结案日收盘{_price(facts.get('close'))}未站上上沿{_price(facts.get('zone_high'))}"
                if facts.get('close') is not None
                else '，结案日收盘价缺失'
            )
            touch_text = '，期间曾进入观察区间' if facts.get('ever_touched') else ''
            return f'判决事实：观察窗口{window_text}{high_text}{touch_text}{close_text}。原支撑确认转为压力，观察哨关闭。'
        if facts['outcome'] == 'INVALIDATED':
            close_text = f"收盘{_price(facts.get('close'))}" if facts.get('close') is not None else '收盘价缺失'
            volume_text = f"，量比{_price(facts.get('volume_ratio'))}" if facts.get('volume_ratio') is not None else ''
            return f'判决事实：观察窗口{window_text}，{close_text}放量站上上沿{_price(facts.get("zone_high"))}{volume_text}。原止损逻辑失效，观察哨关闭。'
        return f'判决事实：观察窗口{window_text}{high_text}，期间从未进入观察区间{_price(facts.get("zone_low"))}–{_price(facts.get("zone_high"))}。观察哨关闭。'
    observed = _retest_elapsed_days(signal, report_date, market)
    if signal.status is SignalStatus.TRIGGERED:
        return f'落库判决：确认破位；已观察{observed}交易日。观察哨关闭。'
    if signal.status is SignalStatus.INVALIDATED:
        return f'落库判决：放量站回，假破位；原止损逻辑失效。已观察{observed}日。观察哨关闭。'
    return f'落库判决：窗口到期未回抽；已观察{observed}交易日。观察哨关闭。'


def _cooldown_note(signal: Signal, market: Mapping[str, Any]) -> str:
    counts = (market.get('recent_stop_counts') or {}).get(signal.symbol) or {}
    current_family = MA_FAMILY.get(signal.strategy_id, signal.strategy_id)
    notes: list[str] = []
    for family, hits in counts.items():
        if family == 'unknown':
            continue
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
    lines = ['环境：']
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
    lines = ['事件：']
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
    stop_settlements: list[Mapping[str, Any]] | None = None,
) -> str:
    names = {str(code): str(name) for code, name in (market.get('name_map') or {}).items()}
    scenario_date = str(market.get('signal_date') or report_date)
    entries = [
        signal for signal in signals
        if signal.signal_role is SignalRole.ENTRY
        and signal.status is SignalStatus.ACTIVE
        and signal.created_at.date().isoformat() == scenario_date
    ]
    active_pool_signals = [
        signal for signal in signals
        if signal.signal_role in {SignalRole.STOP, SignalRole.PROFIT, SignalRole.T}
        and signal.status is SignalStatus.ACTIVE
    ]
    pool_by_key: dict[tuple[str, SignalRole, SignalSide], Signal] = {}
    for signal in active_pool_signals:
        key = (signal.symbol, signal.signal_role, signal.side)
        existing = pool_by_key.get(key)
        if existing is None or signal.created_at > existing.created_at:
            pool_by_key[key] = signal
    pool_signals = list(pool_by_key.values())
    signal_date = _text(market.get('signal_date') or report_date)
    settlements_by_signal = {
        str(row['signal_id']): row for row in (stop_settlements or [])
        if _text(row.get('trade_date')) == signal_date
    }
    breaks = [
        signal for signal in signals
        if signal.signal_role is SignalRole.STOP
        and signal.status is SignalStatus.TRIGGERED
        and signal.closed_at and signal.closed_at.date().isoformat() == signal_date
    ]
    observations = [signal for signal in signals if signal.signal_role is SignalRole.RETEST_CONFIRM and signal.status is SignalStatus.ACTIVE]
    judgments = [
        signal for signal in signals
        if signal.signal_role is SignalRole.RETEST_CONFIRM
        and signal.status in {SignalStatus.TRIGGERED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}
        and signal.closed_at and signal.closed_at.date().isoformat() == signal_date
    ]
    market_for_projection = dict(market)
    market_for_projection.setdefault('report_date', report_date)
    lines = [f'【{report_date} 交易计划】', render_header(market_for_projection, report_date)]
    trigger_review = list(market.get('execution_review') or [])
    lines.append(f'A0 信号日（{signal_date}）触发回顾')
    lines.append(render_trigger_review(trigger_review, names))
    ev_negative = render_ev_negative(market)
    if ev_negative:
        lines.append(ev_negative)
    candidate_note = '不合并同票多策略'
    if (
        market.get('watchlist_limit') is not None
        and market.get('pool_total') is not None
        and int(market['watchlist_limit']) < int(market['pool_total'])
    ):
        candidate_note += f"，池子 {market['pool_total']}→{market['watchlist_limit']}"
    lines.append(f'候选（{len({signal.symbol for signal in entries})}只，{candidate_note}）：')
    lines.extend(_render_candidate_groups(entries, pool_signals, market, names) or ['无'])
    lines.append(_candidate_audit_note(market, len({signal.symbol for signal in entries})))
    observed_symbols = {signal.symbol for signal in observations}
    shown_pool_signals = [signal for signal in pool_signals if signal.symbol not in observed_symbols]
    pool_symbols = {signal.symbol for signal in shown_pool_signals} | {signal.symbol for signal in observations}
    lines.append(f'池内票区（{len(pool_symbols)}只）：')
    lines.extend(_render_pool_groups(
        shown_pool_signals, signals, market, names, report_date, observations,
        protection_states=market.get('protection_states'),
    ) or ['无'])
    lines.append(f'破位区（{len(breaks)}条）：')
    lines.extend([
        _render_break_with_settlement(signal, names, settlements_by_signal.get(signal.id), market)
        for signal in breaks
    ] or ['无'])
    # 收盘处理计数只统计信号日当天的结算，避免历史结算混入当日总数
    today_settlements = list(settlements_by_signal.values())
    settlement_counts: dict[str, int] = {}
    for row in today_settlements:
        key = str(row.get('settlement_outcome') or 'PENDING_DATA')
        settlement_counts[key] = settlement_counts.get(key, 0) + 1
    observation_closures = len(judgments)
    closing_total = len(today_settlements) + observation_closures
    correction_scene_labels = {
        'same_day_recover': '当日收复',
        'after_execute_now': '执行后反抽',
        'retest_invalidated': '假破位确认',
        'retest_pass': '假破位确认',
        'post_break_retest': '执行后反抽',
    }
    corrections = list(market.get('correction_events') or [])
    corrections = [
        {
            **dict(row),
            'scene': correction_scene_labels.get(
                row.get('scene') or row.get('trigger'),
                row.get('scene') or row.get('trigger'),
            ),
        }
        for row in corrections
        if (row.get('scene') or row.get('trigger')) in {
            'same_day_recover', 'after_execute_now', 'retest_invalidated', 'retest_pass', 'post_break_retest'
        }
    ]
    corrections = _merge_correction_rows(corrections)
    summary_text = '｜'.join(
        f'{_SETTLEMENT_OUTCOME_LABELS.get(key, key)}{count}'
        for key, count in sorted(settlement_counts.items())
    ) + f'｜观察哨结案{observation_closures}'
    lines.append(f'收盘处理（{closing_total}条）：{summary_text}')
    lines.extend([
        (
            f"{names.get(str(row['symbol']), str(row['symbol']))}"
            f"（{row['symbol']}）｜{row.get('scene') or row.get('trigger')}｜{row['notice']}"
        )
        for row in corrections
    ] or ['无'])
    lines.append(f'观察区（{len(observations)}个）：')
    lines.extend([
        _retest_observation_line(signal, report_date, market, names)
        for signal in observations
    ] or ['无'])
    lines.append(f'判决区（{len(judgments)}条）：')
    judgment_status = {
        'TRIGGERED': '确认破位',
        'EXPIRED': '无反抽',
        'INVALIDATED': '假破位',
    }
    judgment_lines = []
    for signal in judgments:
        qualifier = _BREAK_QUALIFIER_LABELS.get(signal.break_qualifier or '', signal.break_qualifier or '')
        qualifier_text = f'（{qualifier}）' if qualifier else ''
        line = f'判决：{_signal_name(signal, names)}（{signal.symbol}） {judgment_status.get(signal.status.value, signal.status.value)}{qualifier_text}'
        try:
            observed_days = _retest_elapsed_days(signal, report_date, market)
        except (TypeError, ValueError):
            observed_days = 0
        line += f"｜已观察{observed_days}天"
        if signal.status_reason:
            reason = _STATUS_REASON_LABELS.get(signal.status_reason, signal.status_reason)
            line += f"｜原因 {reason}"
        line += '｜' + _retest_judgment_note_with_market(signal, report_date, market)
        judgment_lines.append(line)
    lines.extend(judgment_lines or ['无'])
    closing = render_closing(market)
    if closing:
        lines.append(closing)
    lines.append('个人观点，仅供参考。')
    return '\n'.join(lines)
