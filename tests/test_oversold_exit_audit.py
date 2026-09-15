"""中科飞测卖出自查回归：量能口径、极端超卖降级、硬止损保护。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.signal_plan import build_volume_snapshot
from src.analyzers.timing_engine import TimingEngine
from src.data_layer.stock_data import calc_tech_indicators
from src.push.templates import _tech


def _oversold_tech(current_price: float = 304.03):
    kline = []
    for index in range(60):
        close = 340.72 - index * 0.3
        kline.append({
            "date": f"2026-06-{(index % 28) + 1:02d}",
            "open": close + 0.5,
            "high": close + 1.0,
            "low": close - 1.5,
            "close": close,
            "volume": 1_000_000,
        })
    return {
        "current_price": current_price,
        "ma5": 308.54,
        "ma10": 327.89,
        "ma20": 340.76,
        "ma5_prev": 309.20,
        "ma5_prev2": 309.60,
        "volume_ratio": 2.29,
        "volume_ratio_source": "行情接口",
        "kline": kline,
        "tech_signals": {
            "vote": "偏空",
            "vote_score": -1.5,
            "rsi": 35.16,
            "rsi6": 24.28,
            "kdj": {"k": 17.33, "d": 25.30, "j": 1.39},
            "bollinger": {"position": "below", "lower": 292.85},
            "obv": {"obv": 29594, "change_5": -1338, "direction": "下行"},
            "volume_snapshot": {
                "volume_ratio": 2.29,
                "volume_ratio_source": "行情接口",
                "volume_ratio_raw": 2.29,
                "volume_vs_prev_day": 0.34,
            },
        },
    }


def _engine_with(tech, position=None):
    engine = TimingEngine(backtest_mode=False)
    engine._fetch_tech_data = lambda code, mode="defend": tech
    engine._get_paired_position = lambda code: position
    return engine


class TestVolumeAudit:
    def test_snapshot_keeps_ratio_source_and_prev_day_ratio(self):
        kline = [{"volume": 1_000_000} for _ in range(10)]
        snapshot = build_volume_snapshot({
            "kline": kline,
            "today_volume": 340_000,
            "volume_ratio": 2.29,
            "volume_ratio_source": "行情接口",
        })
        assert snapshot.volume_ratio_source == "行情接口"
        assert snapshot.volume_ratio_raw == 2.29
        assert snapshot.volume_vs_prev_day == 0.34

    def test_calc_tech_indicators_publishes_obv_direction(self):
        kline = []
        for index in range(40):
            close = 350 - index
            kline.append({
                "close": close,
                "high": close + 1,
                "low": close - 1,
                "volume": 1_000_000,
            })
        tech = calc_tech_indicators(
            kline,
            volume_ratio=1.60,
            volume_ratio_source="行情接口",
        )
        assert tech["obv"]["direction"] == "下行"
        assert tech["volume_ratio_source"] == "行情接口"

    def test_missing_volume_is_not_silent(self):
        assert "量比:数据未取到" in _tech({"tech_signals": {}})


class TestOversoldExitAudit:
    def test_extreme_oversold_downgrades_technical_weakness(self):
        tech = _oversold_tech()
        engine = _engine_with(tech)
        signals = engine.check_exit_signals("688361", "中科飞测", "defend")
        weakness = [signal for signal in signals if signal.exit_type == "技术走弱"]
        assert len(weakness) == 1
        assert weakness[0].urgency == "观察"
        assert "非执行级" in weakness[0].reason
        assert "RSI6超卖(24.3)" in weakness[0].reason
        assert "KDJ-J超卖(1.4)" in weakness[0].reason
        assert "布林下轨" in weakness[0].reason
        assert "OBV下行" in weakness[0].reason
        assert "口径:行情接口" in weakness[0].reason
        assert "较前日0.34x" in weakness[0].reason

    def test_hard_stop_still_fires_inside_oversold(self):
        tech = _oversold_tech(current_price=280.00)
        position = {
            "paired_z": 282.10,
            "z_reference": 282.10,
            "entry_type": "价量突破",
            "paired_w_low": 340.00,
            "paired_w_high": 350.00,
        }
        engine = _engine_with(tech, position)
        signals = engine.check_exit_signals("688361", "中科飞测", "defend")
        breakdown = [signal for signal in signals if signal.exit_type == "破位止损"]
        assert len(breakdown) == 1
        assert breakdown[0].urgency in ("紧急", "重要")
        assert "硬触发" in breakdown[0].reason

    def test_report_displays_kdj_obv_and_volume_audit(self):
        text = _tech(_oversold_tech())
        assert "KDJ:17.3/25.3/J1.4" in text
        assert "OBV:下行" in text
        assert "口径:行情接口" in text
        assert "较前日0.34x" in text
