from pathlib import Path

from pointtable.core.auction import AuctionPattern, classify_auction
from pointtable.core.lhb import evaluate_lhb
from pointtable.core.sector import SectorState, classify_sector_state, load_theme_map, order_pool


def test_auction_patterns():
    assert classify_auction(10.0, 9.8, 1.2) is AuctionPattern.WEAK_TO_STRONG
    assert classify_auction(10.0, 10.2, 0.5) is AuctionPattern.STRONG_TO_WEAK
    assert classify_auction(10.0, 10.0, 1.0) is AuctionPattern.NEUTRAL


def test_lhb_veto_rules():
    assert evaluate_lhb({'seat-a'}, 0.1).bad
    assert evaluate_lhb(set(), 0.4).bad
    assert not evaluate_lhb(set(), 0.2).bad


def test_sector_states_and_pool_order():
    assert classify_sector_state(2.0, 3, 3) is SectorState.MAINLINE
    assert classify_sector_state(0.5, -1, 1) is SectorState.ROTATIONAL
    assert classify_sector_state(-1.0, -2, 0) is SectorState.RETREATING
    rows = [
        {'symbol': 'weak', 'broken': True, 'upstream': False, 'first_line': False},
        {'symbol': 'strong', 'broken': False, 'upstream': True, 'first_line': True},
    ]
    assert [row['symbol'] for row in order_pool(rows)] == ['strong', 'weak']


def test_theme_map_overrides():
    path = Path.cwd() / 'test_theme_map.yaml'
    path.write_text('overrides:\n  300001: AI\n', encoding='utf-8')
    try:
        assert load_theme_map(path) == {'300001': 'AI'}
    finally:
        path.unlink(missing_ok=True)
