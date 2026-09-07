"""
【Phase3】估值透镜 — 回归测试

核心案例（用户实测批评——Pushplus 报告 8 大缺陷的 3 个方向性误判）：
  兆易创新 603986：PE 33.9 + 净利 68.57 亿 + 同比 +1091.5% + 研报一致买入，
    却被 4 票资金流投票标"机构看空(-2票)"
    → value_growth 正向证据 + 资金-研报冲突双标签；
    标签语义修正："机构看X"→"资金看X"（四票源不测度研报共识）
  长光华芯 688048：净利 +238% 但仅 3034 万（低基数反转），PE(TTM) 1155.5 倍
    → valuation_bubble【veto】：为远期故事付百倍溢价，
      突破/追强买入理由 X 无法与之共存
  中科飞测 688361：战略性亏损（合同负债较年初 +66.3%、存货 +26%）
    → strategic_loss【warn】：订单/备货加速，与商业模式烧钱严格区分
  芯原股份 688521：商业模式亏损（无订单证据）+ 确认追强 → veto；
    低吸型策略 → warn + 重乘数
  中际旭创 300308：防守模式禁追强 + 估值透镜基本面强
    → 闸门不放开（防守纪律优先），但冲突显式披露可复盘
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import src.analyzers.valuation_lens as vl
from src.analyzers.valuation_lens import (
    DEFAULT_LENS_CONFIG,
    _consensus_label,
    assemble_valuation_lens,
    attach_analyst_to_institutional,
    evaluate_valuation_lens,
    fetch_profit_abs,
    fetch_valuation_snapshot,
    reset_valuation_lens_state,
)
from src.analyzers.signal_plan import build_execution_plan
from src.push.templates import _fundamental_line, _institutional


# ============================================================
# 数据构造
# ============================================================

def _kline(bars=80, base=90.0, vol=1_000_000, day_range=3.0):
    kline = []
    for i in range(bars):
        close = base + (i % 7) * 0.4
        kline.append({
            "date": f"2026-06-{(i % 28) + 1:02d}",
            "open": close - 0.5, "high": close + day_range / 2,
            "low": close - day_range / 2, "close": close,
            "volume": vol, "amount": vol * close,
        })
    return kline


def _tech_data(**overrides):
    data = {
        "current_price": 101.01,
        "ma5": 97.0, "ma10": 94.0, "ma20": 92.0, "ma25": 88.0,
        "ma25_prev": 88.5, "prev_close": 88.2, "today_open": 89.0,
        "volume_ratio": 2.0, "turnover_rate": 4.8,
        "kline": _kline(),
        "recent_high": 105.0,
    }
    data.update(overrides)
    return data


# 兆易创新：PE 33.9 / 净利 +1091.5% / 68.57 亿 / 研报一致买入 / 资金票 -2
_ZHAOYI_FUND = {"profit_yoy": 1091.5, "deducted_yoy": 950.0, "profit_abs": 68.57}
_ZHAOYI_VAL = {"pe_ttm": 33.9, "pb": 7.2, "pe_source": "TTM"}
_ZHAOYI_ANALYST = {
    "buy": 12, "outperform": 3, "neutral": 1, "reduce": 0, "sell": 0, "total": 16,
    "consensus_label": "看多", "sources": ["stock_research_report_em"],
}

# 长光华芯：PE 1155.5 / 净利 +238% / 仅 0.3034 亿
_CHANGGUANG_FUND = {"profit_yoy": 238.0, "deducted_yoy": 210.0, "profit_abs": 0.3034}
_CHANGGUANG_VAL = {"pe_ttm": 1155.5, "pb": 16.4, "pe_source": "TTM"}

# 中科飞测：亏损 + 合同负债 +66.3% + 存货 +26%（战略性亏损）
_FCE_FUND = {"profit_yoy": -10.0, "deducted_yoy": -12.0, "profit_abs": -0.85}
_FCE_BAL = {"contract_liab_change_pct": 66.3, "inventory_change_pct": 26.0}

# 芯原股份：亏损 -6.12 亿，无订单证据（商业模式亏损）
_VERI_FUND = {"profit_yoy": 5.0, "deducted_yoy": 3.0, "profit_abs": -6.12}


def _plan_kwargs(entry_type="价量突破", **overrides):
    kwargs = dict(
        entry_type=entry_type,
        benchmark_price=94.0,
        stop_loss=83.5,
        target_range=[109.0, 116.0],
        tech_data=_tech_data(),
        sector_status="main_trend",
        hypothesis_x="放量站上MA25，突破位回踩不破",
    )
    kwargs.update(overrides)
    return kwargs


@pytest.fixture(autouse=True)
def _clean_lens_cache():
    reset_valuation_lens_state()
    yield
    reset_valuation_lens_state()


# ============================================================
# 评估器（纯函数）：五大用户案例
# ============================================================

class TestEvaluateLensCases:

    def test_zhaoyi_value_growth_with_flow_analyst_conflict(self):
        """兆易创新：低估值真增长 + 资金-研报冲突 → 正向证据+冲突标记，不否决。"""
        verdict = evaluate_valuation_lens(
            _ZHAOYI_FUND, _ZHAOYI_VAL, _ZHAOYI_ANALYST, None,
            entry_type="价量突破", flow_vote=-2,
        )
        assert verdict["verdict"] == "pass"          # 正向证据同样不加分不否决
        assert "value_growth" in verdict["tags"]
        assert "analyst_flow_conflict" in verdict["tags"]
        assert verdict["risk_multiplier"] == 1.0
        assert "PE(TTM)33.9" in verdict["note"]
        assert "68.57" in verdict["note"]            # 净利双口径

    def test_changguang_bubble_veto(self):
        """长光华芯：PE 1155 倍 + 净利仅 0.30 亿 → 估值泡沫 veto + 低基数反转。"""
        verdict = evaluate_valuation_lens(
            _CHANGGUANG_FUND, _CHANGGUANG_VAL, None, None, entry_type="确认追强",
        )
        assert verdict["verdict"] == "veto"
        assert "valuation_bubble" in verdict["tags"]
        assert "low_base_reversal" in verdict["tags"]
        assert any("百倍溢价" in r for r in verdict["reasons"])
        assert any("低基数" in r for r in verdict["reasons"])

    def test_zhongkefeice_strategic_loss_warn_not_veto(self):
        """中科飞测：合同负债 +66.3% → 战略性亏损（warn），追高也不否决。"""
        verdict = evaluate_valuation_lens(
            _FCE_FUND, None, None, _FCE_BAL, entry_type="确认追强",
        )
        assert verdict["verdict"] == "warn"
        assert "strategic_loss" in verdict["tags"]
        assert "model_loss" not in verdict["tags"]
        assert verdict["risk_multiplier"] == 0.6
        assert any("战略性亏损" in r for r in verdict["reasons"])
        assert any("合同负债" in r for r in verdict["reasons"])

    def test_verisilicon_model_loss_veto_for_chase(self):
        """芯原股份：无订单证据亏损 + 确认追强 → veto（产业逻辑证伪 X）。"""
        verdict = evaluate_valuation_lens(
            _VERI_FUND, None, None, None, entry_type="确认追强",
        )
        assert verdict["verdict"] == "veto"
        assert "model_loss" in verdict["tags"]
        assert any("出厂拒绝" in r for r in verdict["reasons"])

    def test_verisilicon_model_loss_warn_for_dip(self):
        """芯原股份 + 套利低吸（左侧）→ 不否决，warn + 重乘数 0.5。"""
        verdict = evaluate_valuation_lens(
            _VERI_FUND, None, None, None, entry_type="套利低吸",
        )
        assert verdict["verdict"] == "warn"
        assert verdict["risk_multiplier"] == 0.5

    def test_elevated_pe_display_only(self):
        """PE 60-300（科技股常态）→ 仅标注估值偏高，不降级。"""
        verdict = evaluate_valuation_lens(
            {"profit_yoy": 30.0, "profit_abs": 10.0},
            {"pe_ttm": 120.0, "pb": 5.0, "pe_source": "TTM"}, None, None,
        )
        assert verdict["verdict"] == "pass"
        assert "valuation_elevated" in verdict["tags"]

    def test_bubble_with_real_profit_is_warn(self):
        """PE ≥300 但盈利体量大（≥2亿）→ 泡沫 warn（非低基数 veto）。"""
        verdict = evaluate_valuation_lens(
            {"profit_yoy": 240.0, "profit_abs": 8.0},
            {"pe_ttm": 400.0, "pb": 9.0, "pe_source": "TTM"}, None, None,
        )
        assert verdict["verdict"] == "warn"
        assert "valuation_bubble" in verdict["tags"]
        assert verdict["risk_multiplier"] == 0.5

    def test_disabled_lens_passes(self):
        cfg = {"valuation_lens": {"enabled": False}}
        verdict = evaluate_valuation_lens(
            _CHANGGUANG_FUND, _CHANGGUANG_VAL, None, None, config=cfg,
        )
        assert verdict["verdict"] == "pass"
        assert verdict["tags"] == []

    def test_all_data_missing_passes(self):
        """数据全缺 → 放行（不产生假估值结论）。"""
        verdict = evaluate_valuation_lens(None, None, None, None)
        assert verdict["verdict"] == "pass"
        assert verdict["tags"] == []


# ============================================================
# signal_plan 集成：出厂拒绝 / 降级
# ============================================================

class TestExecutionPlanLensGate:

    def test_changguang_bubble_rejected_at_factory(self):
        """长光华芯式泡沫+低基数 → 价量突破信号出厂即拒绝（留痕）。"""
        tech = _tech_data(valuation_lens={
            "code": "688048", "name": "长光华芯",
            "valuation": _CHANGGUANG_VAL, "analyst": None, "balance": None,
            "fundamental": _CHANGGUANG_FUND,
        })
        plan = build_execution_plan(**{**_plan_kwargs(), "tech_data": tech})
        assert plan.execute is False
        assert plan.valuation_rejected is True
        assert plan.hypothesis_rejected is False
        assert any("估值透镜" in r and "泡沫" in r for r in plan.rejection_reasons)
        assert plan.valuation["verdict"]["verdict"] == "veto"

    def test_verisilicon_model_loss_rejects_chase_plan(self):
        """芯原式模式性亏损 + 确认追强 → 出厂拒绝。"""
        tech = _tech_data(valuation_lens={
            "code": "688521", "name": "芯原股份",
            "valuation": None, "analyst": None, "balance": None,
            "fundamental": _VERI_FUND,
        })
        plan = build_execution_plan(**{**_plan_kwargs(entry_type="确认追强"),
                                       "tech_data": tech})
        assert plan.execute is False
        assert plan.valuation_rejected is True
        assert any("模式性亏损" in r for r in plan.rejection_reasons)

    def test_verisilicon_model_loss_dip_not_rejected(self):
        """同票 + 套利低吸（左侧）→ 不拒绝，warn 降级 + 风险乘数。"""
        tech = _tech_data(valuation_lens={
            "code": "688521", "name": "芯原股份",
            "valuation": None, "analyst": None, "balance": None,
            "fundamental": _VERI_FUND,
        })
        plan = build_execution_plan(**{**_plan_kwargs(entry_type="套利低吸"),
                                       "tech_data": tech})
        assert plan.execute is True
        assert plan.valuation_rejected is False
        assert plan.risk_multipliers.get("valuation_warn") == 0.5
        assert any("估值降级" in d for d in plan.confidence_details)

    def test_strategic_loss_warn_downgrades(self):
        """中科飞测式战略亏损 → warn 乘数 0.6 + 硬约束注记。"""
        tech = _tech_data(valuation_lens={
            "code": "688361", "name": "中科飞测",
            "valuation": None, "analyst": None, "balance": _FCE_BAL,
            "fundamental": _FCE_FUND,
        })
        plan = build_execution_plan(**{**_plan_kwargs(), "tech_data": tech})
        assert plan.execute is True
        assert plan.risk_multipliers.get("valuation_warn") == 0.6
        assert any("战略性亏损" in n for n in plan.hard_constraint_notes)

    def test_zhaoyi_value_growth_not_rejected(self):
        """兆易式低估值真增长 + 资金-研报冲突 → 不否决不降级（证据展示）。"""
        tech = _tech_data(valuation_lens={
            "code": "603986", "name": "兆易创新",
            "valuation": _ZHAOYI_VAL, "analyst": _ZHAOYI_ANALYST,
            "balance": None, "fundamental": _ZHAOYI_FUND,
        })
        plan = build_execution_plan(**{**_plan_kwargs(), "tech_data": tech})
        assert plan.execute is True
        assert plan.valuation_rejected is False
        assert "valuation_warn" not in plan.risk_multipliers

    def test_no_lens_unchanged(self):
        """无透镜数据 → 行为不变（兼容回退）。"""
        plan = build_execution_plan(**_plan_kwargs())
        assert plan.execute is True
        assert plan.valuation is None
        assert plan.valuation_rejected is False
        assert plan.risk_multipliers.get("valuation_warn") is None


# ============================================================
# unified_engine：追强闸门冲突披露（中际旭创案例）
# ============================================================

class TestChaseBlockedConflictDisclosure:

    def _blockers(self, market_mode, lens):
        from src.orchestrator.unified_engine import _strategy_blockers
        tech = {"valuation_lens": lens}
        return _strategy_blockers(market_mode, "rotational", tech)

    def test_strong_fundamental_chase_block_disclosed(self):
        """防守模式拦追强 + 估值透镜基本面强 → 冲突显式披露（不放开闸门）。"""
        lens = {
            "valuation": _ZHAOYI_VAL, "analyst": None, "balance": None,
            "fundamental": _ZHAOYI_FUND,
            "verdict": {"tags": ["value_growth"]},
        }
        blockers = self._blockers("defend", lens)
        chase = [b for b in blockers if b.startswith("确认追强")]
        assert len(chase) == 1
        assert "仅进攻模式启用" in chase[0]           # 闸门仍是拦截（纪律优先）
        assert "⚠基本面冲突" in chase[0]              # 但冲突必须可见
        assert "踏空风险" in chase[0]

    def test_analyst_bullish_chase_block_disclosed(self):
        lens = {
            "valuation": None, "balance": None, "fundamental": None,
            "analyst": _ZHAOYI_ANALYST,
            "verdict": {"tags": []},
        }
        blockers = self._blockers("defend", lens)
        chase = [b for b in blockers if b.startswith("确认追强")]
        assert "研报共识看多" in chase[0]

    def test_weak_fundamental_no_conflict_noise(self):
        """基本面弱/无数据 → 维持原 blocker 文案（不制造噪音）。"""
        blockers = self._blockers("defend", None)
        chase = [b for b in blockers if b.startswith("确认追强")]
        assert chase == ["确认追强: 仅进攻模式启用"]

    def test_attack_mode_unaffected(self):
        """进攻模式不走该分支（原逻辑不变）。"""
        blockers = self._blockers("attack", {
            "verdict": {"tags": ["value_growth"]},
        })
        assert all("⚠基本面冲突" not in b for b in blockers)


# ============================================================
# 标签语义修正 + 双标签渲染
# ============================================================

class TestLabelSemantics:

    def test_vote_labels_renamed_to_flow(self, monkeypatch):
        """4 票源标签改"资金看X"（不测度研报共识，诚实命名）。

        ⚠️ test_timing_engine 在模块导入期用全局桩替换 score_institutional_holding
        （本文件按字母序晚于它导入）。importlib.reload 在同一命名空间重执行
        模块代码，恢复真实实现（与 test_institutional_fund_flow 同一手法）。
        """
        import importlib
        import src.analyzers.institutional_scorer as inst
        importlib.reload(inst)
        monkeypatches = {
            "_fetch_margin_balance": lambda c: {"vote": 1, "detail": "两融增加", "raw": {}},
            "_fetch_lhb_institutional": lambda c: {"vote": 1, "detail": "机构净买", "raw": {}},
            "_fetch_main_force_flow": lambda c: {"vote": 0, "detail": "中性", "raw": {}},
            "_fetch_shareholder_count": lambda c: {"vote": 0, "detail": "中性", "raw": {}},
            "_fetch_top10_institutional_ratio": lambda c: None,
        }
        for k, v in monkeypatches.items():
            monkeypatch.setattr(inst, k, v)
        inst._institutional_session_cache.clear()
        result = inst.score_institutional_holding("603986")
        assert result["vote_label"] == "资金看多"
        assert "资金流投票" in result["label_scope"]

    def test_institutional_dual_labels_with_conflict(self):
        """兆易案例渲染：资金票🔴 + 研报共识看多 + ⚠冲突标记（双标签）。"""
        data = {"institutional_holding": {
            "vote_score": -2, "vote_label": "资金看空",
            "bullish_count": 0, "bearish_count": 3, "votes": {},
            "label_scope": "资金流投票(4源,非研报共识)",
            "analyst_consensus": _ZHAOYI_ANALYST,
            "flow_analyst_conflict": True,
        }}
        rendered = _institutional(data)
        assert "资金看空(-2票" in rendered
        assert "非研报共识" in rendered
        assert "研报共识看多" in rendered
        assert "买入12/增持3" in rendered
        assert "⚠资金与研报反向" in rendered

    def test_institutional_without_analyst_renders_flow_only(self):
        data = {"institutional_holding": {
            "vote_score": 2, "vote_label": "资金看多",
            "bullish_count": 2, "bearish_count": 0, "votes": {},
        }}
        rendered = _institutional(data)
        assert "资金看多(+2票" in rendered
        assert "研报" not in rendered

    def test_fundamental_line_lens_double_calibration(self):
        """⑦基本面行：净利双口径（增速+绝对值）+ PE/PB + 泡沫标签。"""
        data = {
            "fundamental": {"profit_yoy": 238.0, "deducted_yoy": 210.0,
                            "report_period": "20260630"},
            "valuation_lens": {
                "valuation": _CHANGGUANG_VAL, "analyst": None, "balance": None,
                "fundamental": _CHANGGUANG_FUND,
                "verdict": {"tags": ["valuation_bubble", "low_base_reversal"],
                            "profit_abs": 0.3034},
            },
        }
        line = _fundamental_line(data)
        assert "净利+238.0%(0.30亿)" in line       # 双口径：增速+绝对值
        assert "PE(TTM)1155.5倍" in line
        assert "PB16.4" in line
        assert "估值泡沫" in line
        assert "低基数反转" in line

    def test_fundamental_line_zhaoyi_value_growth(self):
        data = {
            "fundamental": {"profit_yoy": 1091.5, "deducted_yoy": 950.0,
                            "report_period": "20260630"},
            "valuation_lens": {
                "valuation": _ZHAOYI_VAL, "analyst": None, "balance": None,
                "fundamental": _ZHAOYI_FUND,
                "verdict": {"tags": ["value_growth"], "profit_abs": 68.57},
            },
        }
        line = _fundamental_line(data)
        assert "净利+1091.5%(68.57亿)" in line
        assert "PE(TTM)33.9倍" in line
        assert "低估真增长" in line

    def test_fundamental_line_dynamic_pe_source_annotated(self):
        """兜源动态 PE 必须带口径标注（非 TTM）。"""
        data = {
            "fundamental": {"profit_yoy": 30.0},
            "valuation_lens": {
                "valuation": {"pe_ttm": 45.0, "pb": 3.0, "pe_source": "动态(兜源,非TTM)"},
                "verdict": {"tags": []},
            },
        }
        line = _fundamental_line(data)
        assert "动态(兜源,非TTM)" in line


# ============================================================
# 数据源层：单位归一化 / 优雅降级 / 共识标签
# ============================================================

class TestDataLayer:

    def test_profit_abs_yuan_to_yi_normalization(self, monkeypatch):
        """业绩表以元计（6.857e9 元）→ 归一化为 68.57 亿元。"""
        table = {"603986": {"净利润": 6_857_000_000.0,
                            "净利润-同比增长": 1091.5}}
        monkeypatch.setattr(vl, "_period_candidates", lambda today=None: ["20260630"])
        monkeypatch.setattr(vl, "_fetch_market_table",
                            lambda func, period: table if func == "stock_yjkb_em" else {})
        assert fetch_profit_abs("603986") == pytest.approx(68.57, abs=0.01)

    def test_profit_abs_already_in_yi(self, monkeypatch):
        table = {"688048": {"净利润": 0.3034}}
        monkeypatch.setattr(vl, "_fetch_market_table",
                            lambda func, period: table if func == "stock_yjkb_em" else {})
        assert fetch_profit_abs("688048") == pytest.approx(0.3034, abs=1e-6)

    def test_fetchers_fail_gracefully_without_akshare(self):
        """无 akshare 环境：全部返回 None（透镜降级，不抛异常）。"""
        assert fetch_valuation_snapshot("688048") is None
        assert vl.fetch_analyst_consensus("688048") is None
        assert vl.fetch_balance_sheet_growth("688048") is None

    def test_assemble_returns_none_when_all_sources_fail(self, monkeypatch):
        monkeypatch.setattr(vl, "fetch_valuation_snapshot", lambda c, cfg=None: None)
        monkeypatch.setattr(vl, "fetch_analyst_consensus", lambda c, cfg=None: None)
        monkeypatch.setattr(vl, "fetch_balance_sheet_growth", lambda c, cfg=None: None)
        monkeypatch.setattr(vl, "fetch_profit_abs", lambda c, cfg=None: None)
        assert assemble_valuation_lens("688048", "长光华芯") is None

    def test_assemble_combines_sources_with_profit_abs(self, monkeypatch):
        monkeypatch.setattr(vl, "fetch_valuation_snapshot",
                            lambda c, cfg=None: dict(_CHANGGUANG_VAL))
        monkeypatch.setattr(vl, "fetch_analyst_consensus", lambda c, cfg=None: None)
        monkeypatch.setattr(vl, "fetch_balance_sheet_growth", lambda c, cfg=None: None)
        monkeypatch.setattr(vl, "fetch_profit_abs", lambda c, cfg=None: 0.3034)
        lens = assemble_valuation_lens("688048", "长光华芯")
        assert lens is not None
        assert lens["valuation"]["pe_ttm"] == 1155.5
        assert lens["fundamental"]["profit_abs"] == 0.3034
        assert lens["verdict"]["verdict"] == "veto"       # 组装即评估（中性口径）

    def test_consensus_label_rules(self):
        cfg = DEFAULT_LENS_CONFIG
        assert _consensus_label({"buy": 12, "outperform": 3, "total": 16}, cfg) == "看多"
        assert _consensus_label({"buy": 1, "outperform": 1, "neutral": 8, "total": 10}, cfg) == "中性"
        assert _consensus_label({"reduce": 5, "sell": 2, "total": 8}, cfg) == "看空"
        assert _consensus_label({"total": 0}, cfg) == "无数据"
        # 少量买入（<3 家）不足以判看多
        assert _consensus_label({"buy": 2, "outperform": 0, "total": 2}, cfg) == "中性"

    def test_attach_analyst_no_vote_mixing(self):
        """分析师共识旁路注入：不混票（vote_score 不变），只加标签维度。"""
        inst = {"vote_score": -2, "vote_label": "资金看空", "votes": {}}
        lens = {
            "analyst": _ZHAOYI_ANALYST,
            "verdict": {"tags": ["analyst_flow_conflict"]},
        }
        attach_analyst_to_institutional(inst, lens)
        assert inst["vote_score"] == -2                    # 资金票不被研报共识稀释
        assert inst["analyst_consensus"] == _ZHAOYI_ANALYST
        assert inst["flow_analyst_conflict"] is True


# ============================================================
# 配置
# ============================================================

class TestConfig:

    def test_yaml_lens_block_loaded(self):
        from src.config_models import load_config
        timing = (load_config("timing.yaml") or {}).get("timing", {}) or {}
        lens = timing.get("valuation_lens") or {}
        assert lens.get("enabled") is True
        assert float(lens.get("bubble_pe_threshold", 0)) == 300.0
        assert float(lens.get("low_base_profit_threshold", 0)) == 2.0

    def test_config_thresholds_override(self):
        cfg = {"valuation_lens": {"bubble_pe_threshold": 100.0}}
        verdict = evaluate_valuation_lens(
            {"profit_yoy": 240.0, "profit_abs": 8.0},
            {"pe_ttm": 120.0, "pe_source": "TTM"}, None, None, config=cfg,
        )
        # 阈值 100：PE120 ≥ 100 且盈利体量 ≥2亿 → bubble warn
        assert "valuation_bubble" in verdict["tags"]
        assert verdict["verdict"] == "warn"
