"""
P2-6: timing_engine 单元测试（纯算法，不依赖网络）
"""
import pytest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os
os.environ["TQDM_DISABLE"] = "1"

# 跳过 akshare 依赖
import src.analyzers.institutional_scorer as _inst
_inst.score_institutional_holding = lambda c, *args, **kwargs: {
    "vote_score": 0, "vote_label": "skip", "votes": {},
    "bullish_count": 0, "bearish_count": 0, "neutral_count": 4, "stale": False
}

from src.analyzers.timing_engine import get_backtest_timing_engine
from src.utils.numbers import safe_float, safe_int


class TestSafeFloat:
    """P2-2: safe_float 单元测试"""

    def test_int(self):
        assert safe_float(123) == 123.0

    def test_float(self):
        assert safe_float(123.45) == 123.45

    def test_string(self):
        assert safe_float("123.45") == 123.45

    def test_string_with_comma(self):
        assert safe_float("1,234.56") == 1234.56

    def test_string_with_percent(self):
        assert safe_float("12.5%") == 12.5

    def test_none(self):
        assert safe_float(None) == 0.0

    def test_empty_string(self):
        assert safe_float("") == 0.0

    def test_invalid_string(self):
        assert safe_float("abc") == 0.0

    def test_default(self):
        assert safe_float(None, default=-1.0) == -1.0

    def test_safe_int(self):
        assert safe_int("123.7") == 123
        assert safe_int(None) == 0


class TestStopLossCalc:
    """止损价计算单元测试"""

    def test_dirty_volume_blocks_entry_and_records_reason(self):
        te = get_backtest_timing_engine()
        kline = [{"volume": 100, "close": 10.0} for _ in range(61)]
        tech_data = {
            "kline": kline,
            "today_volume": 2000,
            "volume_ratio": 0.8,
        }
        te._fetch_tech_data = lambda code, mode: tech_data

        signals = te.check_entry_signals("688008", "测试", "defend")

        assert signals == []
        assert te._tech_data_full["688008"]["volume_data_valid"] is False
        assert "量能口径冲突" in te._tech_data_full["688008"]["entry_blocked_reason"]

    def test_stop_loss_with_below_support(self):
        """有下方支撑时，止损价=支撑×0.97"""
        te = get_backtest_timing_engine()
        # 构造 tech_data
        tech_data = {
            'current_price': 100.0,
            'ma5': 98.0,
            'ma10': 95.0,
            'ma20': 90.0,
            'kline': [{'close': 100, 'open': 99, 'high': 101, 'low': 99, 'volume': 1000}] * 30,
        }
        slc = te.calculate_stop_loss('688008', tech_data)
        # chosen_support 应为 ma5=98（最高下方支撑）
        assert slc.chosen_support == 98.0
        # stop_loss_price = 98 × 0.97 = 95.06
        assert abs(slc.stop_loss_price - 95.06) < 0.1

    def test_stop_loss_fallback(self):
        """无下方支撑时，fallback用现价×0.95"""
        te = get_backtest_timing_engine()
        tech_data = {
            'current_price': 100.0,
            'ma5': 105.0,  # 都在现价上方
            'ma10': 110.0,
            'ma20': 115.0,
            'kline': [{'close': 100, 'open': 99, 'high': 101, 'low': 99, 'volume': 1000}] * 30,
        }
        slc = te.calculate_stop_loss('688008', tech_data)
        # fallback: chosen_support = 100 × 0.95 = 95
        assert slc.chosen_support == 95.0


class TestRealtimeKlineSync:
    """盘中现价必须与投票使用的最后一根K线对齐。"""

    def test_star_quote_volume_is_normalized_from_history_unit(self):
        te = get_backtest_timing_engine()
        kline = [
            {"date": "2026-09-01", "close": 100.0, "volume": 1000,
             "amount": 10000000.0},
            {"date": "2026-09-02", "close": 100.5, "volume": 1000,
             "amount": 10050000.0},
        ]
        quote = {"current_price": 101.0, "today_open": 100.6,
                 "volume": 200000.0, "amount": 20100000.0}

        te._sync_last_kline_with_realtime(kline, quote)

        assert kline[-1]["volume"] == 200000.0
        assert kline[-1]["成交量"] == 200000.0
        assert kline[0]["volume"] == 100000.0

    def test_same_day_quote_updates_last_bar(self):
        te = get_backtest_timing_engine()
        kline = [{"date": "2026-09-02", "open": 170.0, "high": 178.0,
                  "low": 169.0, "close": 173.0, "volume": 1000}]
        quote = {
            "current_price": 177.45, "today_open": 171.0,
            "today_high": 178.5, "today_low": 169.5,
            "volume": 2000, "timestamp": "20260902150000",
        }

        synced = te._sync_last_kline_with_realtime(kline, quote)

        assert synced is kline
        assert kline[-1]["close"] == 177.45
        assert kline[-1]["open"] == 171.0
        assert kline[-1]["high"] == 178.5
        assert kline[-1]["low"] == 169.5
        assert kline[-1]["volume"] == 2000

    def test_missing_today_appends_intraday_bar(self):
        te = get_backtest_timing_engine()
        kline = [{"date": "2026-09-01", "open": 170.0, "high": 174.0,
                  "low": 168.0, "close": 173.0, "volume": 1000}]
        quote = {
            "current_price": 177.45, "today_open": 171.0,
            "today_high": 178.5, "today_low": 169.5,
            "volume": 2000, "timestamp": "20260902150000",
        }

        synced = te._sync_last_kline_with_realtime(kline, quote)

        assert synced is kline
        assert len(kline) == 2
        assert kline[-1]["date"] == "2026-09-02"
        assert kline[-1]["close"] == 177.45
        assert kline[-2]["close"] == 173.0


class TestPairBottom:
    """D3: 对子底判定单元测试"""

    def test_pair_bottom_detection(self):
        """价格尾数两位相同=对子"""
        # 12.22 → "1222" → 尾数22相同
        result = False
        price_str = f"{12.22:.2f}".replace(".", "")
        result = (price_str[-2:] == "99" or price_str[-2:] == "00" or
                  (len(price_str) >= 2 and price_str[-2] == price_str[-1]))
        assert result, "12.22 应识别为对子底"

    def test_pair_99(self):
        """199.99 → 99结尾"""
        price_str = f"{199.99:.2f}".replace(".", "")
        assert price_str[-2:] == "99"

    def test_pair_00(self):
        """100.00 → 00结尾"""
        price_str = f"{100.00:.2f}".replace(".", "")
        assert price_str[-2:] == "00"

    def test_non_pair(self):
        """12.34 不是对子"""
        price_str = f"{12.34:.2f}".replace(".", "")
        is_pair = (price_str[-2:] == "99" or price_str[-2:] == "00" or
                   (len(price_str) >= 2 and price_str[-2] == price_str[-1]))
        assert not is_pair, "12.34 不应对子底"


class TestMarketModeAdaptive:
    """B1: 五日线回归单元测试"""

    def test_mode_attack(self):
        """站上5日线+多头排列+派发日低 → attack"""
        from src.loop.market_mode_adaptive import MarketModeAdaptive
        mma = MarketModeAdaptive()
        # 构造K线：5/10/20多头排列，现价>ma5
        closes = [50 + i * 0.1 for i in range(60)]  # 上升趋势
        kline = [{'close': c, 'open': c-0.05, 'high': c+0.1, 'low': c-0.1, 'volume': 1000, 'date': f'2024-01-{i+1:02d}'}
                 for i, c in enumerate(closes)]
        mode = mma.get_mode_for_date(kline[-1]['date'], kline)
        assert mode in ('attack', 'defend'), f"上升趋势应attack/defend，实际{mode}"

    def test_mode_retreat(self):
        """跌破5日线1%缓冲 → retreat"""
        from src.loop.market_mode_adaptive import MarketModeAdaptive
        mma = MarketModeAdaptive()
        # 构造K线：现价远低于ma5
        closes = [60] * 30 + [50]  # 最后一天暴跌
        kline = [{'close': c, 'open': c, 'high': c, 'low': c, 'volume': 1000, 'date': f'2024-01-{i+1:02d}'}
                 for i, c in enumerate(closes)]
        mode = mma.get_mode_for_date(kline[-1]['date'], kline)
        assert mode == 'retreat', f"跌破5日线应retreat，实际{mode}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
