"""
估值透镜（Phase3）— 估值/盈利体量/亏损分型/分析师共识
=====================================================

背景（用户实测批评——Pushplus 报告 8 大缺陷导致 3 个方向性误判）：
  报告只罗列"净利同比"一个数字，估值、盈利体量、订单证据、研报共识
  全部不在系统考量内，导致：
  ① 兆易创新：PE(TTM) 33.9 倍 + 净利 68.57 亿 + 同比 +1091% 的存储龙头，
     被 4 票资金流投票标成"机构看空(-2票)"——四票源（主力/股东/两融/
     龙虎榜）全是短周期资金数据，无一测度研报共识，标签语义挪用；
  ② 长光华芯：净利同比 +238% 看似高增长，实为 3034 万低基数反转，
     PE(TTM) 1155 倍的估值泡沫无任何拦截；
  ③ 中科飞测（合同负债 +66.3%、战略性亏损）与芯原股份（商业模式烧钱）
     被同贴"机构偏空"，未做亏损分型；
  ④ 中际旭创：扣非高增 + PE 合理的产业龙头被"禁追强"静默拦截，
     报告无冲突披露，踏空不可复盘。

本模块四职责（与 fundamental_gate 互补：后者管"业绩在不在暴雷"，
本模块管"增长是真的吗 / 估值配得上吗 / 亏损是什么性质 /
资金票与研报共识是否反向"）：

  1. 估值分档：
     - PE(TTM) ≥ bubble_pe_threshold(300) 且 净利绝对值 < low_base(2亿)
       → valuation_bubble【veto】：为远期故事支付百倍溢价 + 低基数反转，
         突破/追强的买入理由 X 无法与之共存（长光华芯案例）。
     - PE(TTM) ≥ 300 但盈利体量 ≥ 2亿 → bubble warn + 重风险乘数
       （泡沫是事实，但大盈利体量的持续性争议留给 warn 级）。
     - PE(TTM) ≥ elevated_pe(60) → valuation_elevated【仅标注】：科技股常态，
       只展示不打折——宽进严出，避免把"常态偏贵"当"泡沫"。
     - PE < 60 且 同比 ≥ 50% 且 净利 ≥ 5亿 → value_growth【正向证据】：
       低估值真增长（兆易案例），作为资金空票的反向证据呈现，
       不加分不否决——正向定性同样留给事实，防止系统性吹票。
  2. 低基数反转（low_base_reversal）：同比 ≥ 100% 且 净利 < 2亿
     → 增速数字失去"成长"含义（3034 万 +238% ≠ 高增长），
       报告双口径（增速+绝对值），该增速不得作为基本面正向理由。
  3. 亏损分型：净利 ≤ 0 时——
     - 合同负债较期初 ≥ 30% 或 存货 ≥ 20% → strategic_loss【warn】：
       在手订单/备货加速的战略性亏损（中科飞测），披露不否决；
     - 无订单证据 → model_loss：追高型策略（确认追强/价量突破）veto
       （买入理由建立在"产业逻辑兑现"上，模式性亏损直接证伪 X），
       低吸/抄底型降为 warn + 重乘数（芯原案例）。
  4. 资金-分析师冲突（analyst_flow_conflict）：资金票 ≤ -1 而分析师共识
     看多（买入+增持占比 ≥ 60% 或 ≥ 3 家）→ 冲突标记：双标签呈现，
     禁止单标签定性（兆易案例）。资金票是"节奏"，研报共识是"方向"，
     两者反向时报告必须同时展示，而不是取其一。

数据源（全部优雅降级 + session 缓存 + 可 monkeypatch）：
  - ak.stock_value_em(symbol)                  个股估值：PE-TTM/PB/总市值
  - ak.stock_zh_a_spot_em()                    全市场快照兜底（口径标注"动态"）
  - 业绩快报/报表"净利润"列                    复用 fundamental_gate 已拉全市场表
                                              → 零额外 API 调用
  - ak.stock_balance_sheet_by_report_em(symbol) 合同负债/存货（最新 vs 年初）
  - ak.stock_research_report_em(symbol, ...)   个股研报近 90 天评级计数
  ⚠️ PE/研报共识是当下快照，资产负债表是报告期级——推送必须带口径。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 复用 fundamental_gate 的表缓存与工具（同一份全市场业绩表，零额外调用）
from .fundamental_gate import (
    _to_float,
    _period_candidates,
    _fetch_market_table,
)

# ============================================================
# 配置默认值（config/timing.yaml: valuation_lens: 可覆盖）
# ============================================================

DEFAULT_LENS_CONFIG = {
    "enabled": True,
    # 估值分档
    "bubble_pe_threshold": 300.0,       # PE(TTM) ≥ 该值 → 泡沫
    "elevated_pe_threshold": 60.0,      # PE(TTM) ≥ 该值 → 估值偏高（仅标注）
    "value_pe_threshold": 60.0,         # PE < 该值 且真增长 → value_growth
    "real_growth_yoy": 50.0,            # value_growth 要求的最低净利同比(%)
    "real_profit_threshold": 5.0,       # value_growth 要求的净利体量（亿元）
    # 低基数反转
    "low_base_yoy": 100.0,              # 同比 ≥ 该值 且低体量 → 低基数反转
    "low_base_profit_threshold": 2.0,   # 净利绝对值 < 该值（亿元）→ 低体量
    # 亏损分型
    "strategic_contract_liab_pct": 30.0,  # 合同负债较期初 ≥ 该值(%) → 订单证据
    "strategic_inventory_pct": 20.0,      # 存货较期初 ≥ 该值(%) → 备货证据
    "model_loss_risk_multiplier": 0.5,    # model_loss warn 级风险乘数
    "bubble_warn_risk_multiplier": 0.5,   # 泡沫(warn 级)风险乘数
    "warn_risk_multiplier": 0.6,          # 其余 warn 级风险乘数
    # 资金-分析师冲突
    "consensus_buy_ratio": 60.0,         # 买入+增持占比 ≥ 该值(%) → 分析师看多
    "consensus_min_votes": 3,            # 或 买入+增持 ≥ N 家 → 分析师看多
    "conflict_flow_score": -1,           # 资金票 ≤ 该值 且分析师看多 → 冲突
    # 追强闸门冲突披露（unified_engine 消费）
    "chase_blocked_hint": True,
    # 缓存
    "cache_ttl_seconds": 3600,
}

# 追高型策略：买入理由建立在"产业逻辑/趋势延续"上
CHASE_LIKE_ENTRIES = {"确认追强", "价量突破"}

_VETO_VERDICT = "veto"
_WARN_VERDICT = "warn"
_PASS_VERDICT = "pass"


# ============================================================
# session 缓存
# ============================================================

_valuation_cache: Dict[str, Optional[Dict]] = {}
_valuation_cache_ts: Dict[str, float] = {}
_analyst_cache: Dict[str, Optional[Dict]] = {}
_analyst_cache_ts: Dict[str, float] = {}
_balance_cache: Dict[str, Optional[Dict]] = {}
_balance_cache_ts: Dict[str, float] = {}
# 兜源全市场快照（一次拉取，所有股票共享；不能每股一拉全市场表）
_spot_market_cache: Optional[Dict[str, Dict]] = None
_spot_market_cache_ts: float = 0.0
_lock = threading.Lock()


def _lens_cfg(config: Optional[Dict]) -> Dict:
    cfg = dict(DEFAULT_LENS_CONFIG)
    if isinstance(config, dict):
        lens = config.get("valuation_lens") or {}
        if isinstance(lens, dict):
            cfg.update({k: v for k, v in lens.items() if v is not None})
    return cfg


def _cache_ttl(config: Optional[Dict]) -> int:
    return int(_lens_cfg(config).get("cache_ttl_seconds", 3600))


def _item(value: Any) -> Any:
    """numpy 标量 → python 标量（JSON 落库安全）"""
    try:
        if hasattr(value, "item"):
            return value.item()
    except Exception:
        pass
    return value


def _column(row: Dict, candidates: List[str]) -> Any:
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return _item(row[c])
    return None


# ============================================================
# 数据源 1：估值快照（PE-TTM / PB / 总市值）
# ============================================================

def fetch_valuation_snapshot(code: str, config: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """
    个股估值快照。主源 ak.stock_value_em（PE-TTM 口径），
    兜底 ak.stock_zh_a_spot_em 全市场快照（市盈率-动态口径，标注差异）。
    全部失败返回 None（透镜降级，不产生假估值结论）。
    """
    code = str(code).zfill(6)
    ttl = _cache_ttl(config)
    now = time.time()
    with _lock:
        if code in _valuation_cache and now - _valuation_cache_ts.get(code, 0) < ttl:
            cached = _valuation_cache[code]
            return dict(cached) if cached else None

    result: Optional[Dict[str, Any]] = None
    sources: List[str] = []

    # 主源：东财个股估值（PE-TTM / 市净率 / 总市值）
    try:
        import akshare as ak
        df = ak.stock_value_em(symbol=code)
        if df is not None and not getattr(df, "empty", True):
            row = {k: _item(v) for k, v in df.iloc[0].items()}
            pe = _to_float(_column(row, ["市盈率TTM", "市盈率(TTM)", "市盈率-动态", "市盈率"]))
            pb = _to_float(_column(row, ["市净率", "市净率TTM", "PB"]))
            cap = _to_float(_column(row, ["总市值"]))
            if pe is not None or pb is not None:
                result = {
                    "pe_ttm": pe,
                    "pb": pb,
                    "market_cap": cap,          # 亿元（东财口径）
                    "pe_source": "TTM",
                }
                sources.append("stock_value_em")
    except Exception as e:
        logger.debug("估值主源 %s 失败: %s", code, str(e)[:80])

    # 兜底：全市场快照（市盈率-动态——不是 TTM，口径必须标注）
    # 会话级共享缓存：全市场表只拉一次，不能每只股票都拉一遍全表
    if result is None:
        global _spot_market_cache, _spot_market_cache_ts
        try:
            with _lock:
                spot_fresh = (
                    _spot_market_cache is not None
                    and now - _spot_market_cache_ts < ttl
                )
            if not spot_fresh:
                import akshare as ak
                df = ak.stock_zh_a_spot_em()
                market: Dict[str, Dict] = {}
                if df is not None and not getattr(df, "empty", True):
                    code_col = "代码" if "代码" in df.columns else "股票代码"
                    for _, r in df.iterrows():
                        c = str(r.get(code_col, "")).zfill(6)
                        if c:
                            market[c] = {k: _item(v) for k, v in r.items()}
                with _lock:
                    _spot_market_cache = market
                    _spot_market_cache_ts = time.time()
            with _lock:
                row = (_spot_market_cache or {}).get(code)
            if row:
                pe = _to_float(_column(row, ["市盈率-动态", "市盈率TTM"]))
                pb = _to_float(_column(row, ["市净率"]))
                cap = _to_float(_column(row, ["总市值"]))
                if pe is not None or pb is not None:
                    result = {
                        "pe_ttm": pe,
                        "pb": pb,
                        "market_cap": cap,
                        "pe_source": "动态(兜源,非TTM)",
                    }
                    sources.append("stock_zh_a_spot_em")
        except Exception as e:
            logger.debug("估值兜源 %s 失败: %s", code, str(e)[:80])

    if result is not None:
        result["sources"] = sources
    with _lock:
        _valuation_cache[code] = result
        _valuation_cache_ts[code] = now
    return result


# ============================================================
# 数据源 2：分析师共识（研报评级分布）
# ============================================================

def fetch_analyst_consensus(code: str, config: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """
    个股分析师共识：近 90 天研报评级计数（买入/增持/中性/减持/卖出）。
    主源 ak.stock_research_report_em（东财个股研报列表）。
    全部失败返回 None（共识维度缺省，不阻塞资金投票）。
    """
    code = str(code).zfill(6)
    ttl = _cache_ttl(config)
    now = time.time()
    with _lock:
        if code in _analyst_cache and now - _analyst_cache_ts.get(code, 0) < ttl:
            cached = _analyst_cache[code]
            return dict(cached) if cached else None

    counts = {"买入": 0, "增持": 0, "中性": 0, "减持": 0, "卖出": 0}
    total = 0
    sources: List[str] = []

    try:
        import akshare as ak
        end = datetime.now()
        start = end - timedelta(days=90)
        df = ak.stock_research_report_em(
            symbol=code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        )
        if df is not None and not getattr(df, "empty", True):
            rating_col = None
            for c in df.columns:
                if "评级" in str(c):
                    rating_col = c
                    break
            if rating_col is not None:
                for v in df[rating_col].astype(str):
                    v = v.strip()
                    for key in counts:
                        if key in v:
                            counts[key] += 1
                            total += 1
                            break
                sources.append("stock_research_report_em")
    except Exception as e:
        logger.debug("研报共识 %s 拉取失败: %s", code, str(e)[:80])

    if not sources or total == 0:
        with _lock:
            _analyst_cache[code] = None
            _analyst_cache_ts[code] = now
        return None

    result = {
        "buy": counts["买入"],
        "outperform": counts["增持"],
        "neutral": counts["中性"],
        "reduce": counts["减持"],
        "sell": counts["卖出"],
        "total": total,
        "sources": sources,
    }
    result["consensus_label"] = _consensus_label(result, _lens_cfg(config))
    with _lock:
        _analyst_cache[code] = result
        _analyst_cache_ts[code] = now
    return result


def _consensus_label(consensus: Dict[str, Any], cfg: Dict) -> str:
    """分析师共识标签：看多/中性/看空/无数据（与资金投票标签相互独立）"""
    bull = int(consensus.get("buy", 0) or 0) + int(consensus.get("outperform", 0) or 0)
    bear = int(consensus.get("reduce", 0) or 0) + int(consensus.get("sell", 0) or 0)
    total = int(consensus.get("total", 0) or 0)
    if total <= 0:
        return "无数据"
    ratio = bull / total * 100.0
    min_votes = int(cfg.get("consensus_min_votes", 3) or 3)
    buy_ratio = float(cfg.get("consensus_buy_ratio", 60.0))
    if bull >= min_votes and ratio >= buy_ratio:
        return "看多"
    if bear >= min_votes and bear / total * 100.0 >= buy_ratio:
        return "看空"
    return "中性"


# ============================================================
# 数据源 3：资产负债表订单证据（合同负债 / 存货 较年初）
# ============================================================

def _em_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    if code.startswith(("0", "2", "3")):
        return f"SZ{code}"
    return f"BJ{code}"


def fetch_balance_sheet_growth(code: str, config: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """
    合同负债/存货 较年初变化（%）——战略性亏损的订单证据。
    ak.stock_balance_sheet_by_report_em(symbol="SH600xxx")，报告期级数据。
    失败返回 None（亏损分型退化为 model_loss 判定路径的"无证据"）。
    """
    code = str(code).zfill(6)
    ttl = _cache_ttl(config)
    now = time.time()
    with _lock:
        if code in _balance_cache and now - _balance_cache_ts.get(code, 0) < ttl:
            cached = _balance_cache[code]
            return dict(cached) if cached else None

    result: Optional[Dict[str, Any]] = None
    try:
        import akshare as ak
        df = ak.stock_balance_sheet_by_report_em(symbol=_em_symbol(code))
        if df is not None and not getattr(df, "empty", True):
            # 列名为英文（东财报表接口）：REPORT_DATE / CONTRACT_LIAB / INVENTORY
            col_map = {}
            for c in df.columns:
                cu = str(c).upper()
                if cu == "REPORT_DATE" or cu == "REPORT_DATE_NAME":
                    col_map["date"] = c
                elif cu == "CONTRACT_LIAB":
                    col_map["contract_liab"] = c
                elif cu == "INVENTORY":
                    col_map["inventory"] = c
            if "date" not in col_map:
                return None
            df_sorted = df.sort_values(col_map["date"], ascending=False)
            latest = df_sorted.iloc[0]
            latest_date = str(_item(latest[col_map["date"]]))
            latest_year = int(str(latest_date)[:4])

            def _pct(col_key: str) -> Optional[float]:
                if col_key not in col_map:
                    return None
                latest_v = _to_float(latest[col_map[col_key]])
                if latest_v is None:
                    return None
                # 年初基准：上一份年报（上一年 12-31 报告期，找不到则用最早一份）
                base_v: Optional[float] = None
                for i in range(1, min(len(df_sorted), 8)):
                    prev_date = str(_item(df_sorted.iloc[i][col_map["date"]]))
                    if prev_date[:4] == str(latest_year - 1) and prev_date[4:8] == "1231":
                        base_v = _to_float(df_sorted.iloc[i][col_map[col_key]])
                        break
                if base_v is None:
                    base_v = _to_float(df_sorted.iloc[min(len(df_sorted) - 1, 7)][col_map[col_key]])
                if base_v is None or base_v == 0:
                    return None
                return (latest_v - base_v) / abs(base_v) * 100.0

            result = {
                "report_date": latest_date[:10].replace("-", "").replace("/", ""),
                "contract_liab_change_pct": _pct("contract_liab"),
                "inventory_change_pct": _pct("inventory"),
                "sources": ["stock_balance_sheet_by_report_em"],
            }
            if result["contract_liab_change_pct"] is None and result["inventory_change_pct"] is None:
                result = None
    except Exception as e:
        logger.debug("资产负债表 %s 拉取失败: %s", code, str(e)[:80])

    with _lock:
        _balance_cache[code] = result
        _balance_cache_ts[code] = now
    return result


# ============================================================
# 净利润绝对值（亿元）——复用 fundamental_gate 全市场业绩表
# ============================================================

def fetch_profit_abs(code: str, config: Optional[Dict] = None) -> Optional[float]:
    """
    净利润绝对值（亿元）。从 fundamental_gate 已缓存的全市场业绩快报/报表
    读"净利润"列（零额外 API 调用）。单位归一化：业绩表通常以"元"计，
    若绝对值 ≥ 1e4 视为元 → 换算亿元；否则视为已是亿元。
    """
    code = str(code).zfill(6)
    try:
        for func_name in ("stock_yjkb_em", "stock_yjbb_em"):
            for period in _period_candidates():
                table = _fetch_market_table(func_name, period)
                row = table.get(code)
                if not row:
                    continue
                raw = _column(row, [
                    "净利润", "净利润-净利润", "归属净利润", "归母净利润",
                ])
                value = _to_float(raw)
                if value is None:
                    continue
                if abs(value) >= 1e4:
                    value = value / 1e8   # 元 → 亿元
                return round(value, 4)
    except Exception as e:
        logger.debug("净利绝对值 %s 读取失败: %s", code, str(e)[:80])
    return None


# ============================================================
# 评估器（纯函数，可独立测试）
# ============================================================

def evaluate_valuation_lens(
    fundamental: Optional[Dict[str, Any]],
    valuation: Optional[Dict[str, Any]],
    analyst: Optional[Dict[str, Any]],
    balance: Optional[Dict[str, Any]],
    entry_type: str = "",
    config: Optional[Dict] = None,
    flow_vote: Optional[int] = None,
) -> Dict[str, Any]:
    """
    估值透镜评估（纯函数：入参全为数据 dict，无 IO）。

    Returns:
        {
          "verdict": "pass" | "warn" | "veto",
          "tags": [str],           # valuation_bubble / low_base_reversal /
                                   # strategic_loss / model_loss / value_growth /
                                   # valuation_elevated / analyst_flow_conflict
          "reasons": [str],
          "risk_multiplier": float,
          "note": str,             # 一行摘要（推送⑦基本面行）
          "profit_abs": float|None,# 净利绝对值（亿元），速照缺省时 None
        }
    """
    cfg = _lens_cfg(config)
    if not cfg.get("enabled", True):
        return {"verdict": _PASS_VERDICT, "tags": [], "reasons": [],
                "risk_multiplier": 1.0, "note": "", "profit_abs": None}

    tags: List[str] = []
    reasons: List[str] = []
    verdict = _PASS_VERDICT

    fund = fundamental if isinstance(fundamental, dict) else {}
    val = valuation if isinstance(valuation, dict) else {}
    ana = analyst if isinstance(analyst, dict) else {}
    bal = balance if isinstance(balance, dict) else {}

    profit_yoy = _to_float(fund.get("profit_yoy"))
    profit_abs = _to_float(fund.get("profit_abs"))
    if profit_abs is None:
        profit_abs = _to_float(val.get("profit_abs"))
    pe = _to_float(val.get("pe_ttm"))
    pb = _to_float(val.get("pb"))

    bubble_pe = float(cfg.get("bubble_pe_threshold", 300.0))
    elevated_pe = float(cfg.get("elevated_pe_threshold", 60.0))
    value_pe = float(cfg.get("value_pe_threshold", 60.0))
    low_base_profit = float(cfg.get("low_base_profit_threshold", 2.0))
    low_base_yoy = float(cfg.get("low_base_yoy", 100.0))

    # ---------- ② 估值分档 ----------
    if pe is not None:
        if pe >= bubble_pe and (profit_abs is None or profit_abs < low_base_profit):
            tags.append("valuation_bubble")
            verdict = _VETO_VERDICT
            reasons.append(
                f"估值泡沫：PE(TTM){pe:.0f}倍"
                + (f" 而净利仅{profit_abs:.2f}亿（低基数）" if profit_abs is not None else " 净利体量缺失按低基数处理")
                + "——为远期故事支付百倍溢价，突破/追强买入理由无法与之共存"
            )
        elif pe >= bubble_pe:
            tags.append("valuation_bubble")
            if verdict == _PASS_VERDICT:
                verdict = _WARN_VERDICT
            reasons.append(
                f"估值泡沫警示：PE(TTM){pe:.0f}倍（盈利体量{profit_abs:.1f}亿，"
                "高估值依赖增长持续兑现）"
            )
        elif pe >= elevated_pe:
            tags.append("valuation_elevated")
            reasons.append(f"估值偏高：PE(TTM){pe:.0f}倍（科技股常态区间，仅标注）")
        elif (
            profit_yoy is not None and profit_yoy >= float(cfg.get("real_growth_yoy", 50.0))
            and profit_abs is not None and profit_abs >= float(cfg.get("real_profit_threshold", 5.0))
        ):
            tags.append("value_growth")
            reasons.append(
                f"低估值真增长：PE(TTM){pe:.0f}倍 + 净利{profit_yoy:+.0f}%（{profit_abs:.1f}亿）"
                "——资金空票时作为反向证据呈现"
            )

    # ---------- ③ 低基数反转 ----------
    if (
        profit_yoy is not None and profit_yoy >= low_base_yoy
        and profit_abs is not None and profit_abs < low_base_profit
    ):
        if "low_base_reversal" not in tags:
            tags.append("low_base_reversal")
            reasons.append(
                f"低基数反转：净利同比{profit_yoy:+.0f}% 但仅{profit_abs:.2f}亿——"
                "增速数字不构成成长证据（分母太低）"
            )
            if verdict == _PASS_VERDICT:
                verdict = _WARN_VERDICT

    # ---------- ④ 亏损分型 ----------
    if profit_abs is not None and profit_abs <= 0:
        contract_chg = _to_float(bal.get("contract_liab_change_pct"))
        inventory_chg = _to_float(bal.get("inventory_change_pct"))
        strategic_contract = float(cfg.get("strategic_contract_liab_pct", 30.0))
        strategic_inventory = float(cfg.get("strategic_inventory_pct", 20.0))
        has_order_evidence = (
            (contract_chg is not None and contract_chg >= strategic_contract)
            or (inventory_chg is not None and inventory_chg >= strategic_inventory)
        )
        if has_order_evidence:
            tags.append("strategic_loss")
            if verdict == _PASS_VERDICT:
                verdict = _WARN_VERDICT
            evidence_parts = []
            if contract_chg is not None:
                evidence_parts.append(f"合同负债较年初{contract_chg:+.0f}%")
            if inventory_chg is not None:
                evidence_parts.append(f"存货较年初{inventory_chg:+.0f}%")
            reasons.append(
                f"战略性亏损：净利{profit_abs:.2f}亿，但{'、'.join(evidence_parts)}——"
                "订单/备货加速的产品验证期亏损（与商业模式烧钱不同）"
            )
        else:
            tags.append("model_loss")
            reasons.append(
                f"商业模式亏损：净利{profit_abs:.2f}亿 且无订单证据"
                "（合同负债/存货未加速）——盈利拐点未验证"
            )
            if entry_type in CHASE_LIKE_ENTRIES:
                verdict = _VETO_VERDICT
                reasons.append(
                    f"[{entry_type}] 买入理由建立在产业逻辑兑现上，"
                    "模式性亏损直接证伪——出厂拒绝"
                )
            elif verdict == _PASS_VERDICT:
                verdict = _WARN_VERDICT

    # ---------- ① 资金-分析师冲突 ----------
    if (
        flow_vote is not None and int(flow_vote) <= int(cfg.get("conflict_flow_score", -1))
        and isinstance(ana, dict) and str(ana.get("consensus_label")) == "看多"
    ):
        tags.append("analyst_flow_conflict")
        bull = int(ana.get("buy", 0) or 0) + int(ana.get("outperform", 0) or 0)
        reasons.append(
            f"资金-分析师冲突：资金票{int(flow_vote):+d} 但研报共识看多"
            f"（买入{ana.get('buy', 0)}/增持{ana.get('outperform', 0)}，共{bull}家）——"
            "资金是节奏、研报是方向，双标签呈现，禁止单标签定性"
        )

    # ---------- 乘数与摘要 ----------
    multiplier = 1.0
    if verdict == _WARN_VERDICT:
        if "valuation_bubble" in tags or "model_loss" in tags:
            multiplier = float(cfg.get("bubble_warn_risk_multiplier", 0.5))
        else:
            multiplier = float(cfg.get("warn_risk_multiplier", 0.6))

    note = _lens_note(fund=fund, val=val, tags=tags, pe=pe, pb=pb, profit_abs=profit_abs)
    return {
        "verdict": verdict,
        "tags": tags,
        "reasons": reasons,
        "risk_multiplier": multiplier,
        "note": note,
        "profit_abs": profit_abs,
    }


def _lens_note(fund: Dict, val: Dict, tags: List[str], pe: Optional[float],
               pb: Optional[float], profit_abs: Optional[float]) -> str:
    """推送用一行摘要（估值双口径：增速+绝对值+PE/PB）"""
    parts: List[str] = []
    profit_yoy = _to_float(fund.get("profit_yoy"))
    if profit_yoy is not None and profit_abs is not None:
        seg = f"净利{profit_yoy:+.1f}%({profit_abs:.2f}亿)"
        if _to_float(fund.get("deducted_yoy")) is not None:
            seg += f"/扣非{_to_float(fund.get('deducted_yoy')):+.1f}%"
        parts.append(seg)
    if pe is not None:
        seg = f"PE(TTM){pe:.1f}倍"
        if pb is not None:
            seg += f"/PB{pb:.1f}"
        source = str(val.get("pe_source") or "")
        if source and source != "TTM":
            seg += f"({source})"
        parts.append(seg)
    label_map = {
        "valuation_bubble": "估值泡沫",
        "valuation_elevated": "估值偏高",
        "value_growth": "低估真增长",
        "low_base_reversal": "低基数反转",
        "strategic_loss": "战略性亏损(订单加速)",
        "model_loss": "商业模式亏损",
        "analyst_flow_conflict": "资金-分析师冲突",
    }
    hits = [label_map[t] for t in tags if t in label_map]
    if hits:
        parts.append("⚠" + "/".join(hits))
    return " | ".join(parts)


# ============================================================
# 组装（timing_engine._fetch_tech_data 调用）
# ============================================================

def assemble_valuation_lens(
    code: str,
    name: str = "",
    fundamental: Optional[Dict[str, Any]] = None,
    flow_vote: Optional[int] = None,
    config: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """
    拉全估值透镜数据并评估（entry_type 中性版，供观察卡展示；
    信号出厂时 signal_plan 按 entry_type 重新评估——追高型策略对
    model_loss / valuation_bubble 更严格）。

    Returns: {
      "code", "name",
      "valuation": {...}|None, "analyst": {...}|None, "balance": {...}|None,
      "verdict": {...},          # evaluate_valuation_lens 输出
    } 或 None（全部数据源失败 → 透镜缺省，闸门放行，不产生假结论）
    """
    cfg = _lens_cfg(config)
    if not cfg.get("enabled", True):
        return None

    code = str(code).zfill(6)
    valuation = fetch_valuation_snapshot(code, config)
    analyst = fetch_analyst_consensus(code, config)
    balance = fetch_balance_sheet_growth(code, config)

    fund = dict(fundamental) if isinstance(fundamental, dict) else {}
    if fund.get("profit_abs") is None:
        profit_abs = fetch_profit_abs(code, config)
        if profit_abs is not None:
            fund["profit_abs"] = profit_abs

    if valuation is None and analyst is None and balance is None and not fund.get("profit_abs"):
        return None

    verdict = evaluate_valuation_lens(
        fund, valuation, analyst, balance,
        entry_type="", config=config, flow_vote=flow_vote,
    )
    return {
        "code": code,
        "name": str(name or ""),
        "valuation": valuation,
        "analyst": analyst,
        "balance": balance,
        "fundamental": fund or None,
        "verdict": verdict,
    }


def attach_analyst_to_institutional(inst: Dict[str, Any], lens: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    把分析师共识旁路注入机构投票结果（渲染层双标签呈现用）。
    注意：分析师共识**不参与资金票计分**——两个维度测的是不同东西
    （资金流=节奏，研报共识=方向），混票会把冲突平均掉，这正是
    "机构看空(-2票)"误导的根源。冲突只标记，不融合。
    """
    if not isinstance(inst, dict) or not isinstance(lens, dict):
        return inst
    analyst = lens.get("analyst")
    if isinstance(analyst, dict):
        inst["analyst_consensus"] = analyst
    tags = ((lens.get("verdict") or {}).get("tags")) or []
    if "analyst_flow_conflict" in tags:
        inst["flow_analyst_conflict"] = True
    return inst


def reset_valuation_lens_state() -> None:
    """清空 session 缓存（测试用）。"""
    global _spot_market_cache, _spot_market_cache_ts
    with _lock:
        _valuation_cache.clear()
        _valuation_cache_ts.clear()
        _analyst_cache.clear()
        _analyst_cache_ts.clear()
        _balance_cache.clear()
        _balance_cache_ts.clear()
        _spot_market_cache = None
        _spot_market_cache_ts = 0.0
