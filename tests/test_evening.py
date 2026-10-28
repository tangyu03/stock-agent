from pointtable.jobs.evening import evening_report


def test_evening_report_marks_missed_stop_without_receipt_gate():
    text = evening_report(
        '10.21',
        {'amount_yi': 8900, 'tier': 'LOW', 'index_level': 3731},
        [{'name': 'A', 'missed_stop_date': '10.14'}],
    )
    assert '【10.21 复盘】' in text
    assert '市场永远是对的' in text
