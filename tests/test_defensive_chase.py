"""
【Phase4 P1-1】防守模式下确认追强降仓可用 — 回归测试

三重证据：蘅东光 9/7 +14.81%、量比 2.01、ADX 48（全池最高单边力度）
却零提示；防守的定义是压缩敞口而非禁用策略。

试探仓比例 = 单笔风险预算 ÷ 止损距离（1% ÷ 3.5% ≈ 29% ≈ 1/3；
蘅东光类高波动标的止损距离 ~10% → 仓位 ~10%）——出处是风险预算，
不是惯例。

验证：蘅东光 9/7 回放应拿到降仓位入场（约 10% 而非 33%）；
9/3 的它不应通过三重门（RSI 过热+减持迹象）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture(autouse=True)
def _no_institutional(monkeypatch):
    import os
    os.environ.setdefault("TQDM_DISABLE", "1")
    import src.analyzers.institutional_scorer as _inst
    monkeypatch.setattr(
        _inst, "score_institutional_holding",
        lambda c, *a, **kw: {"vote_score": 0, "vote_label": "skip",
                             "votes": {}, "bullish_count": 0,
                             "bearish_count": 0, "neutral_count": 4, "stale": False},
    )


def _kline(bars=70, base=399.0, vol=1_000_000, step=0.9):
    """蘅东光式上升 K 线：历史段缓步上行至 461，末根（今日）+14.8% 至 530.77
    —— MA20 趋势与均线字段自洽，小上影线不自带衰竭信号"""
    kline = []
    for i in range(bars - 1):
        close = base + i * step
        kline.append({
            "date": f"2026-08-{(i % 28) + 1:02d}",
            "open": close - 1.2, "high": close + 0.3, "low": close - 1.5,
            "close": close, "volume": vol,
        })
    # 今日 K：+14.81%（昨收 461.9 → 530.77）
    kline.append({
        "date": "2026-09-07",
        "open": 470.0, "high": 535.0, "low": 465.0,
        "close": 530.77, "volume": 2_200_000,
    })
    return kline


def _hengdongguang_9_7_tech(**overrides):
    """蘅东光 9/7 盘中数据：530.77 +14.81%，量比2.01，ADX48，RSI14=64.7
    （K 线自洽：缓升段至 461.9 后当日 +14.8%，MA20 趋势向上）"""
    tech = {
        "current_price": 530.77,
        "recent_high": 524.0,            # 现价 ≥ 高点×0.99 → 创新高（含当日新高）
        "ma5": 470.0, "ma10": 462.0, "ma20": 455.0,
        "ma25": 452.0, "ma25_prev": 450.0, "prev_close": 461.9,
        "volume_ratio": 2.01,
        "adx": 48.0,
        "rsi": 64.7,
        "outer_volume": 862_000, "inner_volume": 632_000,   # 外盘占优（+15.4%）
        "kline": _kline(bars=70, base=399.0),
        "tech_signals": {},
        # 门二基本面：净利 +113.3%（3.05 亿，非低基数）
        "fundamental": {"profit_yoy": 113.3, "profit_abs": 3.05, "report_period": "2026-06"},
        "institutional_holding": {
            "vote_score": 0,
            "votes": {
                "shareholder": {"raw": {"change_pct": -0.069}},  # 户数减少，筹码集中
            },
        },
    }
    tech.update(overrides)
    return tech


def _engine(monkeypatch, tech):
    from src.analyzers.timing_engine import TimingEngine, StopLossCalc
    te = TimingEngine(backtest_mode=True)
    monkeypatch.setattr(te, "_fetch_tech_data", lambda code, mode="defend": tech)
    stop = StopLossCalc(
        stock_code="920045", current_price=tech.get("current_price", 530.77),
        support_candidates=[], chosen_support=470.0,
        stop_loss_price=455.0, resistance=560.0,
    )
    return te, stop


class TestDefensiveChaseGate:
    """三重门：四确认+基本面+板块联动，任何一关不过就不放行"""

    def test_9_7_passes_all_three_gates(self, monkeypatch):
        """蘅东光 9/7：三重门全过 → 确认追强在防守模式降仓放行"""
        from src.analyzers.timing_engine import TimingEngine
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(_hengdongguang_9_7_tech(), "main_trend")
        assert gate["passed"] is True, gate["evidence"]
        assert gate["projected_volume"] is None or gate["projected_volume"] > 0

    def test_9_7_chase_signal_generated_in_defend_mode(self, monkeypatch):
        """防守模式确认追强信号诞生（蘅东光 9/7 零提示 → 有提示）"""
        te, stop = _engine(monkeypatch, _hengdongguang_9_7_tech())
        sig = te._check_momentum_chase("920045", "蘅东光", _hengdongguang_9_7_tech(), stop, "defend", "main_trend")
        assert sig is not None
        assert sig.entry_type == "确认追强"
        assert sig.position_level == "probe"                     # 降仓（试探仓）
        assert sig.defensive_chase is not None
        assert "防守降仓放行" in sig.trigger_reason
        assert "三重门" in sig.trigger_reason

    def test_9_3_rejected_by_rsi_overheat(self, monkeypatch):
        """9/3 形态：RSI 66.6→70（过热）+ 户数分散（减持迹象）→ 三重门拦截"""
        from src.analyzers.timing_engine import TimingEngine
        tech_9_3 = _hengdongguang_9_7_tech(
            rsi=70.5,                                    # 过热（阈值 67）
            institutional_holding={
                "vote_score": 0,
                "votes": {"shareholder": {"raw": {"change_pct": 0.28}}},  # 户数大增=减持迹象
            },
        )
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(tech_9_3, "main_trend")
        assert gate["passed"] is False
        assert any("RSI14未过热" in e for e in gate["evidence"])   # Phase5: 文案带口径(阈值)
        assert any("筹码无分散" in e for e in gate["evidence"])

    def test_9_3_chase_blocked_in_defend_mode(self, monkeypatch):
        te, stop = _engine(monkeypatch, _hengdongguang_9_7_tech(
            rsi=70.5,
            institutional_holding={
                "vote_score": 0,
                "votes": {"shareholder": {"raw": {"change_pct": 0.28}}},
            },
        ))
        sig = te._check_momentum_chase("920045", "蘅东光", _hengdongguang_9_7_tech(
            rsi=70.5,
            institutional_holding={
                "vote_score": 0,
                "votes": {"shareholder": {"raw": {"change_pct": 0.28}}},
            },
        ), stop, "defend", "main_trend")
        assert sig is None

    def test_non_mainline_sector_blocks(self, monkeypatch):
        """门三：板块非主线（联动度缺失）→ 不放行"""
        from src.analyzers.timing_engine import TimingEngine
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(_hengdongguang_9_7_tech(), "rotational")
        assert gate["passed"] is False
        assert any("板块非主线" in e for e in gate["evidence"])

    def test_weak_fundamental_blocks(self, monkeypatch):
        """门二：净利同比不达标 → 不放行"""
        from src.analyzers.timing_engine import TimingEngine
        te = TimingEngine(backtest_mode=True)
        gate = te._defensive_chase_gates(
            _hengdongguang_9_7_tech(fundamental={"profit_yoy": 8.0, "profit_abs": 3.05}),
            "main_trend",
        )
        assert gate["passed"] is False
        assert any("净利同比达标" in e for e in gate["evidence"])

    def test_disabled_flag_restores_old_behavior(self, monkeypatch):
        """回退开关：defensive_chase.enabled=false → 防守模式恢复禁用（踏空成本留档）"""
        te, stop = _engine(monkeypatch, _hengdongguang_9_7_tech())
        _orig_cfg = te._cfg
        te._cfg = lambda *path, default=None: (
            {"enabled": False} if path[:2] == ("defensive_chase",)
            else _orig_cfg(*path, default=default)
        )
        sig = te._check_momentum_chase(
            "920045", "蘅东光", _hengdongguang_9_7_tech(), stop, "defend", "main_trend")
        assert sig is None

    def test_attack_mode_unaffected(self, monkeypatch):
        """进攻模式行为不变（仍是全额度确认追强）"""
        te, stop = _engine(monkeypatch, _hengdongguang_9_7_tech())
        sig = te._check_momentum_chase(
            "920045", "蘅东光", _hengdongguang_9_7_tech(), stop, "attack", "main_trend")
        assert sig is not None
        assert sig.position_level == "spread"
        assert sig.defensive_chase is None


class TestRiskBudgetPosition:
    """试探仓比例 = 单笔风险预算 ÷ 止损距离（每个数字都有出处）"""

    def test_medium_volatility_approximates_one_third(self):
        """中波动：1% ÷ 3.5% ≈ 29% ≈ 原仓位 1/3（1/3 只是近似值）"""
        from src.analyzers.signal_plan import compute_risk_budget_position
        result = compute_risk_budget_position(100.0, 96.5)
        assert result["stop_distance_pct"] == pytest.approx(0.035, abs=0.001)
        assert result["ratio"] == pytest.approx(0.286, abs=0.005)
        assert result["capped"] is False

    def test_hengdongguang_high_volatility_gets_10pct(self):
        """蘅东光类高波动（止损距离 ~10%）→ 仓位 ~10% 而非 33%"""
        from src.analyzers.signal_plan import compute_risk_budget_position
        result = compute_risk_budget_position(520.0, 468.0)      # 距离 10%
        assert result["stop_distance_pct"] == pytest.approx(0.10, abs=0.005)
        assert result["ratio"] == pytest.approx(0.10, abs=0.01)
        assert result["capped"] is False

    def test_cap_at_one_third_for_tiny_stop_distance(self):
        """封顶 1/3：波动越小仓位越大，但封顶（出处=风险预算上限）"""
        from src.analyzers.signal_plan import compute_risk_budget_position
        result = compute_risk_budget_position(100.0, 99.0)      # 距离 1% → 名义 100%
        assert result["capped"] is True
        assert result["ratio"] == pytest.approx(1 / 3, abs=0.01)
        assert "封顶" in result["note"]

    def test_inversion_returns_zero(self):
        """止损倒挂/异常 → 不反推（防毫厘止损杠杆化）"""
        from src.analyzers.signal_plan import compute_risk_budget_position
        assert compute_risk_budget_position(100.0, 105.0)["ratio"] == 0.0
        tiny = compute_risk_budget_position(100.0, 99.95)       # 距离 0.05% < 1% 下限
        assert tiny["ratio"] == 0.0
        assert "毫厘止损" in tiny["note"]

    def test_note_contains_derivation(self):
        """仓位出处自述：预算÷距离=仓位（数字有出处，可审计）"""
        from src.analyzers.signal_plan import compute_risk_budget_position
        result = compute_risk_budget_position(100.0, 96.5)
        assert "风险预算1%÷止损距离3.5%" in result["note"]
        assert "=29%仓位" in result["note"]


class TestSchedulerSizing:
    """调度器落位：防守追强信号按风险预算反推仓位（非 1/3 惯例）"""

    def _chase_sig_dict(self):
        """调度器入参为 dict（engine.py 转换后的形态）"""
        return {
            "stock_code": "920045", "stock_name": "蘅东光",
            "entry_type": "确认追强",
            "trigger_price": 530.77, "entry_trigger_price": 530.77,
            "confidence": "高",
            "note": "防守降仓放行(三重门全过) | 调度: 基准520.00 | RRR2.80",
            "reason": "防守降仓放行(三重门全过)",
            "benchmark_price": 520.0, "rrr_low": 2.8,
            "stop_loss": 468.0,
            "position_level": "probe",
            "market_mode": "defend",
            "hypothesis": {
                "z": 468.0, "y": 520.0, "w": [560.0, 580.0],
                "defensive_chase": {"evidence": [], "projected_volume": 2.0},
            },
            "execution_plan": {
                "benchmark_price": 520.0,
                "combined_risk_multiplier": 1.0,
                "industry_multiplier": 1.0,
                "hypothesis": {"z": 468.0, "y": 520.0, "w": [560.0, 580.0],
                               "defensive_chase": {"evidence": [], "projected_volume": 2.0}},
                "execution_tiers": [
                    {"role": "main", "name": "MA10档", "price": 520.0, "state": "上方",
                     "distance": "上方2.1%", "trigger": "回踩确认"},
                    {"role": "probe", "name": "MA5档", "price": 526.0, "state": "上方",
                     "distance": "上方0.9%", "trigger": "缩量试探"},
                    {"role": "stop", "name": "止损", "price": 468.0, "state": "上方",
                     "distance": "上方12.0%", "trigger": "收盘跌破离场"},
                ],
            },
        }

    def test_defensive_chase_sized_by_risk_budget(self, monkeypatch):
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            entry_signals=[self._chase_sig_dict()], exit_signals=[],
            total_asset=1_000_000, market_mode="defend",
            holdings=[], offline_strategies=[],
        )
        assert scheduled["stats"]["buy_executed"] == 1
        buy = scheduled["buy"][0]
        # 止损距离 (520-468)/520 = 10% → 预算仓位 1%/10% = 10% → 25k/530.77 ≈ 47 股
        # → 不足 1 手 → 试探仓地板 100 股（比例语义保留在 risk_budget_note）
        assert buy.shares == 100
        plan_note = (buy.execution_plan or {}).get("risk_budget_note", "")
        assert "风险预算1%÷止损距离10.0%" in plan_note
        assert "10%仓位" in plan_note

    def test_strategy_key_tags_defensive_chase_for_stats(self, monkeypatch):
        """防守追强信号落库标记 → 分层统计键 确认追强@防守（30笔回退判定的样本源）"""
        from src.decision.live_scheduler import schedule_live_signals
        scheduled = schedule_live_signals(
            entry_signals=[self._chase_sig_dict()], exit_signals=[],
            total_asset=1_000_000, market_mode="defend",
            holdings=[], offline_strategies=[],
        )
        assert scheduled["buy"][0].hypothesis.get("defensive_chase") is True


class TestBlockerDisclosure:
    """观察卡披露：防守模式追强拦截原因带三重门细节（踏空风险显式可见）"""

    def test_blocker_text_discloses_gate_detail(self):
        from src.orchestrator.unified_engine import _strategy_blockers
        tech = _hengdongguang_9_7_tech()
        tech["rsi"] = 70.5          # 卡在门一
        blockers = _strategy_blockers("defend", "main_trend", tech)
        chase = [b for b in blockers if b.startswith("确认追强")]
        assert len(chase) == 1
        assert "三重门" in chase[0]
        assert "RSI14超5%" in chase[0]

    def test_gate_text_passes_silently_when_all_gates_green(self):
        """三重门全过（信号应已生成）→ 拦截文案不带'未过'细节"""
        from src.orchestrator.unified_engine import _strategy_blockers
        blockers = _strategy_blockers("defend", "main_trend", _hengdongguang_9_7_tech())
        chase = [b for b in blockers if b.startswith("确认追强")]
        assert len(chase) == 1
        assert "未过" not in chase[0]
