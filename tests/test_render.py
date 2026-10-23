from pointtable.notify.render import (
    render_closing,
    render_header,
    render_plan,
    render_position,
    render_report,
)


def test_header_slots_skip_when_missing():
    text = render_header({'amount_yi': 8900, 'tier': 'LOW'})
    assert '8900亿' in text
    assert 'MA5' not in text


def test_position_slots_and_terminal_sentiment():
    row = {
        'name': 'A', 'progress_pct': 20, 'up_day': True,
        'volume_ratio': 1.5, 'active_delta': 8, 'stop_low': 9.0,
        'stop_high': 9.2, 'trend': 'UP',
    }
    text = render_position(row)
    assert '信号生效至今 +20%。' in text
    assert '带量的阳线，外盘占优。' in text
    assert '止损9–9.2。' in text
    assert '拿来格局' in text


def test_receipt_coverage_gates_p5_and_c1():
    row = {'name': 'A', 'receipt_coverage': 0.4, 'missed_stop_date': '1.2'}
    assert '侥幸交税' not in render_position(row)
    assert render_closing({'missed_stop_hit': True}, 0.4) == ''
    assert '市场永远是对的' in render_closing({'missed_stop_hit': True}, 0.6)


def test_plan_and_report_shape():
    market = {'amount_yi': 8900, 'tier': 'LOW', 'index_level': 3731, 'zero_plan_day': True, 't_symbols': ['A']}
    text = render_report('10.21', market, [{'name': 'A'}])
    assert '【10.21 复盘】' in text
    assert '开盘先看3731' not in text
    assert '不开新仓' in text
    assert '少动就是赢' in text
