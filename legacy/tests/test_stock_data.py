from src.data_layer.stock_data import _is_a_share_trading_time, _volume_share_factor


def test_premarket_zero_volume_is_not_suspension():
    assert _is_a_share_trading_time("20260904091800") is False


def test_trading_session_zero_volume_is_checked_as_suspension():
    assert _is_a_share_trading_time("20260904093100") is True
    assert _is_a_share_trading_time("20260904133000") is True


def test_weekend_is_not_suspension_check():
    # 2026-09-05 is Saturday.
    assert _is_a_share_trading_time("20260905100000") is False


def test_volume_share_factor_handles_board_specific_tencent_units():
    # Tencent returns shares for this STAR quote but lots for the ChiNext quote.
    assert _volume_share_factor(5_820_278, 584_836_441, 101.61) == 1
    assert _volume_share_factor(31_727, 402_804_616, 128.60) == 100
