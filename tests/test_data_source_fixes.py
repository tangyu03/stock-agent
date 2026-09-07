"""
【C】数据源技术性缺陷整改 — 回归测试（Phase2-C）

核心案例（用户实测批评）：
  1. 金海通 603061（沪市两融标的）被报"无融资余额数据（非两融标的或深市接口失败）"
     → 接口失败的直接证据。修复：官方接口成败追踪 + 东财 datacenter 兜底 +
     区分「非两融标的」与「接口失败」。
  2. 股东户数"增加110%"是报告期级滞后数据（统计截止日距今天数），
     不是当下筹码信号 → 滞后 >90 天不参与投票，展示强制带"报告期"口径。
  3. 主力净流出基于大单拆单算法，机构拆单即可规避 → 票权降 0.5，
     单噪音源不再能翻动机构总分。

akshare 在测试环境不可用 → sys.modules 注入假模块 + monkeypatch
call_ak_with_retry / _fetch_margin_balance_em。
"""
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import pytest

import src.analyzers.institutional_scorer as _inst

# ⚠️ tests/test_timing_engine.py 在模块导入期永久替换 score_institutional_holding
# （全局桩，返回旧形状 dict）。本文件按字母序先于它导入，这里保存真实函数引用，
# 权重/兜底测试直接调用真实实现，不受后续全局桩影响。
_REAL_SCORE = _inst.score_institutional_holding


def _margin_dates():
    """与 _fetch_margin_balance 相同的日期推导（同进程内一致）。"""
    today = datetime.now().strftime("%Y%m%d")
    latest = _inst._find_recent_trading_day(today, skip_today=True)
    prev = _inst._find_recent_trading_day(
        (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    )
    return latest, prev


@pytest.fixture(autouse=True)
def _reset_state():
    _inst._reset_institutional_state()
    _inst._margin_market_cache = {}
    _inst._margin_cache_date = ""
    yield
    _inst._reset_institutional_state()
    _inst._margin_market_cache = {}
    _inst._margin_cache_date = ""


def _install_fake_akshare(monkeypatch, sse_frame=None, szse_frame=None,
                          sse_raise=False, gdhs_frame=None):
    """注入假 akshare 模块 + 拦截 call_ak_with_retry。"""
    fake = types.ModuleType("akshare")

    def _sse(date):
        if sse_raise:
            raise ConnectionError("SSE 接口超时")
        return sse_frame

    def _szse(date):
        return szse_frame

    def _gdhs(symbol):
        return gdhs_frame

    fake.stock_margin_detail_sse = _sse
    fake.stock_margin_detail_szse = _szse
    fake.stock_zh_a_gdhs_detail_em = _gdhs
    fake.stock_yjkb_em = lambda date: pd.DataFrame()
    fake.stock_yjbb_em = lambda date: pd.DataFrame()
    fake.stock_yjyg_em = lambda date: pd.DataFrame()
    monkeypatch.setitem(sys.modules, "akshare", fake)

    def fake_call(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(_inst, "call_ak_with_retry", fake_call)


def _sse_frame_with(codes_bal: dict):
    return pd.DataFrame({
        "标的证券代码": list(codes_bal.keys()),
        "融资余额": list(codes_bal.values()),
    })


# ============================================================
# 1. 两融：接口失败 vs 非两融标的 vs 东财兜底
# ============================================================

class TestMarginSourceFixes:

    def test_em_fallback_recovers_jinhaitong(self, monkeypatch):
        """金海通铁证：沪市官方表拉不到（空表）→ 东财兜底拿到余额序列 →
        正常投票，明细标注（东财兜底）。"""
        latest, prev = _margin_dates()
        _install_fake_akshare(monkeypatch, sse_frame=pd.DataFrame())  # 官方沪市空表
        monkeypatch.setattr(_inst, "_fetch_margin_balance_em", lambda code: {
            latest: 1.05e8, prev: 1.00e8,   # +5% → 看多
        })
        result = _inst._fetch_margin_balance("603061")
        assert result["vote"] == 1
        assert "东财兜底" in result["detail"]
        assert "融资余额增加" in result["detail"]

    def test_official_ok_absent_code_means_non_target(self, monkeypatch):
        """官方表拉取成功但无此券（其他两融标的在场）→ 信息性「非两融标的」，
        不再误报接口失败。"""
        _install_fake_akshare(
            monkeypatch,
            sse_frame=_sse_frame_with({"600519": 9e9}),  # 有其他标的=表拉取成功
        )
        monkeypatch.setattr(_inst, "_fetch_margin_balance_em", lambda code: None)
        result = _inst._fetch_margin_balance("600000")
        assert result["vote"] == 0
        assert "非两融标的" in result["detail"]
        assert "接口失败" not in result["detail"]
        assert result["raw"].get("margin_target") is False

    def test_all_sources_fail_reports_interface_failure(self, monkeypatch):
        """官方接口失败 + 兜底也无数据 → 明确报「两融接口失败」，
        raw 带 data_quality 标记（不再误导性暗示非两融标的）。"""
        _install_fake_akshare(monkeypatch, sse_raise=True)
        monkeypatch.setattr(_inst, "_fetch_margin_balance_em", lambda code: None)
        result = _inst._fetch_margin_balance("603061")
        assert result["vote"] == 0
        assert "两融接口失败" in result["detail"]
        assert "不代表非两融标的" in result["detail"]
        assert result["raw"].get("data_quality") == "margin_source_failed"

    def test_official_path_still_preferred(self, monkeypatch):
        """官方表正常返回该券（不触发兜底）→ 明细无兜底标注。"""
        latest, prev = _margin_dates()
        _install_fake_akshare(monkeypatch, sse_frame=_sse_frame_with({
            "603061": 2.0e8, "600519": 9e9,
        }))
        # 预置 cache_date=今天（避免函数内全清），并预填 prev 日缓存模拟 5 日前数据
        _inst._margin_cache_date = datetime.now().strftime("%Y%m%d")
        _inst._margin_market_cache[prev] = {"603061": 1.9e8}
        result = _inst._fetch_margin_balance("603061")
        assert result["vote"] == 1  # +5.3% → 看多
        assert "东财兜底" not in result["detail"]


# ============================================================
# 2. 股东户数：报告期级滞后数据
# ============================================================

class TestShareholderStaleness:

    def _gdhs_frame(self, as_of: str, latest=42000, prev=40000):
        return pd.DataFrame({
            "股东户数统计截止日": [as_of, "2026-01-15"],
            "股东户数-本次": [latest, prev],
            "股东户数-上次": [prev, 39000],
        })

    def test_stale_report_period_not_voting(self, monkeypatch):
        """统计截止日距今 120 天（>90 天阈值）→ 不参与投票 + 滞后标注。
        （“股东户数增加110%”是过去报告期的变化，不是当下筹码信号）"""
        as_of = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
        _install_fake_akshare(monkeypatch, gdhs_frame=self._gdhs_frame(
            as_of, latest=44000, prev=21000,  # +110% 的量级
        ))
        result = _inst._fetch_shareholder_count("301392")
        assert result["vote"] == 0
        assert "不参与投票" in result["detail"]
        assert "滞后" in result["detail"]
        assert result["raw"].get("stale") is True
        assert result["raw"].get("age_days", 0) > 90

    def test_fresh_report_period_votes_with_period_label(self, monkeypatch):
        """新鲜报告期（30 天前）→ 正常投票，明细带「报告期」口径前缀。"""
        as_of = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _install_fake_akshare(monkeypatch, gdhs_frame=self._gdhs_frame(
            as_of, latest=39000, prev=41000,  # -4.9% → 筹码集中看多
        ))
        result = _inst._fetch_shareholder_count("688008")
        assert result["vote"] == 1
        assert result["detail"].startswith("报告期")
        assert result["raw"].get("as_of")
        assert "stale" not in result["raw"]


# ============================================================
# 3. 主力资金流 / 股东户数 噪音降权
# ============================================================

class TestVoteWeights:

    def test_single_noisy_sources_cannot_flip_total(self, monkeypatch):
        """两融+1、主力-1、股东-1、龙虎榜0：
        旧逻辑总分 -1（偏空）；新逻辑加权 1-0.5-0.5=0 → 中性。
        单靠噪音源（拆单算法/报告期滞后）不再能翻动机构结论。"""
        monkeypatch.setattr(_inst, "_fetch_margin_balance",
                            lambda c: {"vote": 1, "detail": "两融增加", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_lhb_institutional",
                            lambda c: {"vote": 0, "detail": "无榜", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_main_force_flow",
                            lambda c: {"vote": -1, "detail": "主力净流出", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_shareholder_count",
                            lambda c: {"vote": -1, "detail": "户数增加", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_top10_institutional_ratio", lambda c: None)
        result = _REAL_SCORE("603061")
        assert result["vote_score"] == 0
        assert result["vote_score_weighted"] == 0.0
        assert result["vote_weights"]["main_force"] == 0.5
        assert result["vote_weights"]["shareholder"] == 0.5
        assert result["vote_weights"]["north_bound"] == 1.0

    def test_disclosed_sources_keep_full_weight(self, monkeypatch):
        """两融+1、龙虎榜+1（交易所披露口径）→ 加权 2.0 → 机构看多。"""
        monkeypatch.setattr(_inst, "_fetch_margin_balance",
                            lambda c: {"vote": 1, "detail": "两融增加", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_lhb_institutional",
                            lambda c: {"vote": 1, "detail": "机构净买", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_main_force_flow",
                            lambda c: {"vote": 0, "detail": "中性", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_shareholder_count",
                            lambda c: {"vote": 0, "detail": "中性", "raw": {}})
        monkeypatch.setattr(_inst, "_fetch_top10_institutional_ratio", lambda c: None)
        result = _REAL_SCORE("603061")
        assert result["vote_score"] == 2
        assert result["vote_label"] == "机构看多"

    def test_weight_annotation_in_push_template(self):
        """推送④资金行：主力/股东明细带（权重0.5）标注，人工可复核。"""
        from src.push.templates import _institutional
        data = {
            "institutional_holding": {
                "vote_score": 0,
                "vote_label": "机构中性",
                "bullish_count": 1, "bearish_count": 2,
                "votes": {
                    "main_force": {"vote": -1, "detail": "主力净流出", "raw": {}},
                    "shareholder": {"vote": -1, "detail": "户数增加", "raw": {}},
                },
                "vote_weights": {"north_bound": 1.0, "lhb": 1.0,
                                 "main_force": 0.5, "shareholder": 0.5},
            }
        }
        text = _institutional(data)
        assert "权重0.5" in text
        assert "主力" in text and "股东" in text


# ============================================================
# 4. 观察卡七问：基本面行（含数据口径）
# ============================================================

class TestObservationFundamental:

    def test_observation_card_renders_seventh_question(self):
        from src.push.templates import _render_compact_observation_signal
        data = {
            "stock_name": "汇成真空", "stock_code": "301392",
            "current_price": 55.0, "change_pct": -8.0,
            "market_mode": "defend",
            "sector_name": "半导体设备", "sector_status": "main_trend",
            "tech_signals": {},
            "fundamental": {
                "report_period": "20260630",
                "profit_yoy": -55.0, "deducted_yoy": -58.0,
                "forecast_type": "预亏",
                "verdict": {"verdict": "veto", "tags": ["earnings_bomb"], "note": "业绩雷"},
            },
            "note": "买入: 无 | 卖出: 无",
        }
        title, content = _render_compact_observation_signal(data)
        assert "七问" in content
        assert "⑦基本面" in content
        assert "预亏" in content
        assert "业绩雷" in content
        assert "报告期2026-06" in content
