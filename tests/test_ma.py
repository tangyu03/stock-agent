from pathlib import Path

import yaml

from pointtable.core.ma import ma_tomorrow


def _anchors() -> list[dict]:
    path = Path(__file__).parent / 'fixtures' / 'ma_anchors.yaml'
    return yaml.safe_load(path.read_text(encoding='utf-8'))['anchors']


def test_ma_tomorrow_matches_manual_anchors():
    for anchor in _anchors():
        result = ma_tomorrow(anchor['closes'], anchor['n'], anchor['est_close'])
        assert abs(result - anchor['expected']) / anchor['expected'] < 0.005


def test_ma_tomorrow_uses_estimated_close_instead_of_last_close():
    assert ma_tomorrow([10.0, 11.0, 12.0, 13.0], 5, 15.0) == 12.2


def test_ma_tomorrow_rejects_bad_inputs():
    for closes, n, est_close in (([], 2, 1.0), ([1.0], 0, 1.0), ([1.0], 2, -1.0)):
        try:
            ma_tomorrow(closes, n, est_close)
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError')
