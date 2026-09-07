"""
基本面闸门（Phase2-A）— 业绩/盈利质量/财报窗口的出厂前校验
============================================================

背景（用户实测批评）：
  框架"六问"全部是 K 线 + 资金流，零基本面维度——
  ① 发现不了汇成真空的业绩雷（预亏/扣非大幅下滑照样推买点）；
  ② 识别不出澜起"扣非+21% 但净利+72%"的盈利质量差异（投资收益驱动）。

本模块职责（对齐可证伪假说的 X 要素）：
  1. 业绩雷检测（veto 级）——预告类型为 预亏/首亏/续亏/增亏，
     或 扣非同比 / 净利同比 < bomb_deducted_yoy_threshold（默认 -30%）：
     信号在生成阶段即拒绝（execute=False，留痕 signal_rejections）。
     买入理由 X 必须能与基本面共存：业绩在暴雷，"放量突破"不构成买入理由。
  2. 盈利质量降级（warn 级）——净利同比 - 扣非同比 >= quality_gap_pp（默认 30 个百分点）：
     增长主要由投资收益/非经常性损益驱动 → 置信度降一档 + 风险乘数（默认 0.6）。
  3. 财报窗口保护（warn 级）——下一次法定披露日前 report_window_days（默认 7）天内：
     新开仓信号降级 + 标注（防披露跳空，不做硬否决——窗口期也可能是有利催化）。

数据源（全部优雅降级，任一失败不阻塞其余）：
  - ak.stock_yjkb_em(date)  业绩快报（全市场一次拉取，session 缓存）→ 净利同比/营收同比
  - ak.stock_yjbb_em(date)  业绩报表（同上，快报缺失时兜底）
  - ak.stock_yjyg_em(date)  业绩预告（全市场）→ 预告类型/变动幅度
  - ak.stock_financial_abstract(symbol) 扣非净利润（单股，尽力而为）→ 扣非同比

  ⚠️ 业绩数据是报告期级（季报），不是实时数据；本模块把"报告期 + 披露日"
  一并写入快照，消费方（推送/日志）必须展示数据口径，防止把"过去一个
  报告期的变化"当成当下变化误读。
"""
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 配置默认值（config/timing.yaml: fundamental_gate: 可覆盖）
# ============================================================

DEFAULT_FUNDAMENTAL_CONFIG = {
    "enabled": True,
    # 业绩雷：预告类型 ∈ 预亏类，或 扣非/净利同比 < 阈值（%）
    "bomb_forecast_types": ["预亏", "首亏", "续亏", "增亏", "预减"],
    "bomb_yoy_threshold": -30.0,
    # 盈利质量：净利同比 - 扣非同比 ≥ 30 个百分点 → 增长质量低（非经驱动）
    "quality_gap_pp": 30.0,
    # 财报窗口：距下一次法定披露日 ≤ N 天 → warn
    "report_window_days": 7,
    # warn 级风险乘数（与 turnover_hot 0.5 同一乘数体系）
    "warn_risk_multiplier": 0.6,
    # 数据源 session 缓存 TTL（秒）
    "cache_ttl_seconds": 3600,
}

_BOMB_VERDICT = "veto"
_WARN_VERDICT = "warn"
_PASS_VERDICT = "pass"

# 法定披露截止日（A股季报惯例）
_REPORT_DEADLINES = [
    (1, 1, 4, 30),    # 一季报: 当年 4/30 前
    (5, 1, 8, 31),    # 中报:   当年 8/31 前
    (9, 1, 10, 31),   # 三季报: 当年 10/31 前
    (11, 1, 4, 30),   # 年报:   次年 4/30 前
]


# ============================================================
# 数据快照
# ============================================================

@dataclass
class FundamentalSnapshot:
    """个股基本面快照（报告期级数据，注意口径滞后）"""
    code: str
    name: str = ""
    report_period: str = ""            # 报告期，如 "20260630"
    announce_date: str = ""            # 公告日期，如 "2026-08-28"
    profit_yoy: Optional[float] = None    # 净利同比（%）
    deducted_yoy: Optional[float] = None  # 扣非净利同比（%）
    revenue_yoy: Optional[float] = None   # 营收同比（%）
    forecast_type: str = ""               # 预告类型：预增/预亏/首亏/...
    forecast_change_pct: Optional[float] = None  # 预告变动幅度（%）
    forecast_reason: str = ""             # 业绩变动原因
    next_report_date: str = ""            # 下一次法定披露日（YYYY-MM-DD）
    sources: List[str] = field(default_factory=list)  # 命中的数据源

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 数据源（全部可被测试 monkeypatch）
# ============================================================

_snapshot_cache: Dict[str, Dict] = {}
_snapshot_cache_ts: Dict[str, float] = {}
_table_cache: Dict[str, Dict[str, Dict]] = {}  # {period: {code: row-dict}}
_table_cache_ts: Dict[str, float] = {}
_lock = threading.Lock()


def _cache_ttl(config: Optional[Dict]) -> int:
    cfg = (config or {}) if isinstance(config, dict) else {}
    return int(cfg.get("cache_ttl_seconds", DEFAULT_FUNDAMENTAL_CONFIG["cache_ttl_seconds"]))


def _period_candidates(today: Optional[date] = None) -> List[str]:
    """最近两个已结束报告期（YYYYMMDD）。按当前日期推断：
    3月内 → 上年报+三季报；5月内 → 上年报+一季报（一季报刚出/未出）；
    简化策略：返回最近两个自然季度末。"""
    today = today or date.today()
    quarters = [(3, 31), (6, 30), (9, 30), (12, 31)]
    periods: List[date] = []
    for y in (today.year, today.year - 1):
        for m, d in quarters:
            p = date(y, m, d)
            if p < today:
                periods.append(p)
    periods.sort(reverse=True)
    return [p.strftime("%Y%m%d") for p in periods[:2]]


def _table_get_column(row: Dict, candidates: List[str]) -> Any:
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c]
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        v = float(value)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _fetch_market_table(func_name: str, period: str) -> Dict[str, Dict]:
    """全市场业绩表（快报/报表/预告），session 级缓存，失败返回空 dict。"""
    key = f"{func_name}:{period}"
    now = time.time()
    with _lock:
        if key in _table_cache and now - _table_cache_ts.get(key, 0) < 3600:
            return _table_cache[key]
    rows: Dict[str, Dict] = {}
    try:
        import akshare as ak
        func = getattr(ak, func_name, None)
        if func is None:
            return {}
        df = func(date=period)
        if df is None or getattr(df, "empty", True):
            return {}
        for _, row in df.iterrows():
            code = str(_table_get_column(row, ["股票代码", "代码", "symbol"]) or "").zfill(6)
            if code:
                rows[code] = {k: (v if not hasattr(v, "item") else v.item()) for k, v in row.items()}
    except Exception as e:
        logger.debug("业绩表 %s(%s) 拉取失败: %s", func_name, period, str(e)[:80])
        return {}
    with _lock:
        _table_cache[key] = rows
        _table_cache_ts[key] = time.time()
    return rows


def _fetch_deducted_yoy(code: str, period: str) -> Optional[float]:
    """扣非净利同比（单股，尽力而为；同报告期 vs 上年同期）。
    akshare stock_financial_abstract 按单股返回指标表。失败返回 None。"""
    try:
        import akshare as ak
        df = ak.stock_financial_abstract(symbol=code)
        if df is None or getattr(df, "empty", True):
            return None
        # 宽表：每行一个指标，列为报告期。定位"扣除非经常性损益"行
        target_row = None
        for _, row in df.iterrows():
            key = str(row.iloc[0] if len(row) else "")
            if "扣除非经常" in key or "扣非" in key:
                target_row = row
                break
        if target_row is None:
            return None
        periods = [str(c) for c in df.columns[1:]]
        current = _to_float(target_row.get(period)) if period in periods else None
        if current is None:
            return None
        # 上年同期
        prev_period = None
        try:
            py = int(period[:4]) - 1
            prev_period = f"{py}{period[4:]}"
        except Exception:
            return None
        prev = _to_float(target_row.get(prev_period)) if prev_period in periods else None
        if prev is None or prev == 0:
            return None
        return (current - prev) / abs(prev) * 100.0
    except Exception as e:
        logger.debug("扣非数据 %s 拉取失败: %s", code, str(e)[:80])
        return None


def _norm_period(period: str) -> str:
    p = str(period)
    if len(p) == 8 and p.isdigit():
        return f"{p[:4]}-{p[4:6]}-{p[6:]}"
    return p


def _next_report_deadline(today: Optional[date] = None) -> str:
    """下一次法定披露日（惯例推断；个股具体日期以公告为准，误差≤数天）。"""
    today = today or date.today()
    candidates: List[date] = []
    for start_m, start_d, end_m, end_d in _REPORT_DEADLINES:
        # 报告期结束后的截止日
        dl = date(today.year, end_m, end_d)
        if (end_m, end_d) == (4, 30) and today.month >= 11:
            # 年报（次年4/30）在 11-12 月视角下不可用——用一季报次年4/30近似
            dl = date(today.year + 1, 4, 30)
        if dl >= today:
            candidates.append(dl)
    if not candidates:
        candidates.append(date(today.year + 1, 4, 30))
    return min(candidates).isoformat()


def fetch_fundamental_snapshot(code: str, name: str = "",
                               config: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """
    拉取个股基本面快照（session 缓存 1 小时；失败返回 None，不阻塞主链路）。

    Returns: FundamentalSnapshot.as_dict() 或 None
    """
    code = str(code).zfill(6)
    ttl = _cache_ttl(config)
    now = time.time()
    with _lock:
        if code in _snapshot_cache and now - _snapshot_cache_ts.get(code, 0) < ttl:
            cached = dict(_snapshot_cache[code])
            cached["stale"] = True
            return cached

    periods = _period_candidates()
    snap = FundamentalSnapshot(code=code, name=str(name or ""))
    sources: List[str] = []

    # 1) 业绩快报/报表：净利同比 + 营收同比 + 公告日
    for func_name in ("stock_yjkb_em", "stock_yjbb_em"):
        if snap.profit_yoy is not None:
            break
        for period in periods:
            table = _fetch_market_table(func_name, period)
            row = table.get(code)
            if not row:
                continue
            profit = _to_float(_table_get_column(row, [
                "净利润-同比增长", "净利润同比增长", "净利润-净利润同比增长",
                "归母净利润同比增长", "yjbb",
            ]))
            revenue = _to_float(_table_get_column(row, [
                "营业收入-同比增长", "营业收入同比增长", "营收同比增长",
            ]))
            announce = _table_get_column(row, ["最新公告日期", "公告日期"])
            if profit is None and revenue is None:
                continue
            snap.report_period = period
            snap.profit_yoy = profit
            snap.revenue_yoy = revenue
            snap.announce_date = str(announce or "")[:10]
            sources.append(func_name)
            break

    # 2) 业绩预告：预告类型 + 变动幅度
    for period in periods:
        table = _fetch_market_table("stock_yjyg_em", period)
        row = table.get(code)
        if not row:
            continue
        ftype = str(_table_get_column(row, ["预告类型", "预测类型"]) or "")
        fchange = _to_float(_table_get_column(row, [
            "预告净利润变动幅度", "预告变动幅度", "变动幅度",
        ]))
        freason = str(_table_get_column(row, ["业绩变动原因", "变动原因"]) or "")[:60]
        if ftype:
            snap.forecast_type = ftype
            snap.forecast_change_pct = fchange
            snap.forecast_reason = freason
            if not snap.report_period:
                snap.report_period = period
            sources.append("stock_yjyg_em")
            break

    # 3) 扣非同比（单股，尽力而为）
    period_for_deducted = snap.report_period or (periods[0] if periods else "")
    if period_for_deducted:
        deducted = _fetch_deducted_yoy(code, period_for_deducted)
        if deducted is not None:
            snap.deducted_yoy = deducted
            sources.append("stock_financial_abstract")

    if not sources:
        # 全部数据源失败：返回 None（不产生假基本面结论）
        with _lock:
            _snapshot_cache[code] = None
            _snapshot_cache_ts[code] = now
        return None

    snap.sources = sources
    snap.next_report_date = _next_report_deadline()
    result = snap.as_dict()
    result["stale"] = False
    with _lock:
        _snapshot_cache[code] = result
        _snapshot_cache_ts[code] = now
    return result


# ============================================================
# 闸门评估
# ============================================================

def evaluate_fundamental_gate(
    fund: Optional[Dict[str, Any]],
    config: Optional[Dict] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    基本面闸门评估。

    Returns:
        {
          "verdict": "pass" | "warn" | "veto",
          "reasons": [str],        # 人读原因（含数据口径）
          "tags": [str],           # 机器标签：earnings_bomb / low_quality_growth / report_window
          "risk_multiplier": float # warn 级风险乘数（pass=1.0；veto 不用乘数，直接拒绝）
          "note": str,             # 推送用一行摘要
        }
    """
    cfg = dict(DEFAULT_FUNDAMENTAL_CONFIG)
    if isinstance(config, dict):
        fg = config.get("fundamental_gate") or {}
        if isinstance(fg, dict):
            cfg.update({k: v for k, v in fg.items() if v is not None})

    if not cfg.get("enabled", True):
        return {"verdict": _PASS_VERDICT, "reasons": [], "tags": [],
                "risk_multiplier": 1.0, "note": ""}

    if not fund:
        return {"verdict": _PASS_VERDICT,
                "reasons": ["基本面数据缺失（数据源不可用），闸门放行"],
                "tags": ["fundamental_data_missing"],
                "risk_multiplier": 1.0, "note": "基本面:N/A"}

    reasons: List[str] = []
    tags: List[str] = []
    verdict = _PASS_VERDICT

    profit_yoy = _to_float(fund.get("profit_yoy"))
    deducted_yoy = _to_float(fund.get("deducted_yoy"))
    forecast_type = str(fund.get("forecast_type") or "")
    period = _norm_period(fund.get("report_period") or "")

    # ① 业绩雷（veto）
    bomb_types = [str(t) for t in (cfg.get("bomb_forecast_types") or [])]
    bomb_threshold = float(cfg.get("bomb_yoy_threshold", -30.0))
    bomb_hit = False
    if forecast_type and forecast_type in bomb_types:
        bomb_hit = True
        reasons.append(f"业绩雷：预告类型「{forecast_type}」"
                       + (f"（变动{fund.get('forecast_change_pct')}%）" if fund.get("forecast_change_pct") is not None else ""))
    if deducted_yoy is not None and deducted_yoy < bomb_threshold:
        bomb_hit = True
        reasons.append(f"业绩雷：扣非净利同比{deducted_yoy:+.1f}%<{bomb_threshold:.0f}%")
    elif profit_yoy is not None and profit_yoy < bomb_threshold and not forecast_type:
        bomb_hit = True
        reasons.append(f"业绩雷：净利同比{profit_yoy:+.1f}%<{bomb_threshold:.0f}%")
    if bomb_hit:
        tags.append("earnings_bomb")
        verdict = _BOMB_VERDICT

    # ② 盈利质量（warn；veto 已定时不再叠加）
    gap_pp = float(cfg.get("quality_gap_pp", 30.0))
    if verdict != _BOMB_VERDICT and profit_yoy is not None and deducted_yoy is not None:
        gap = profit_yoy - deducted_yoy
        if gap >= gap_pp:
            tags.append("low_quality_growth")
            verdict = _WARN_VERDICT
            reasons.append(
                f"盈利质量低：净利同比{profit_yoy:+.1f}% vs 扣非{deducted_yoy:+.1f}%"
                f"（差{gap:.0f}pp，增长靠投资收益/非经常性损益驱动）"
            )

    # ③ 财报窗口（warn）
    window_days = int(cfg.get("report_window_days", 7))
    next_date = str(fund.get("next_report_date") or "")
    if next_date:
        try:
            nd = datetime.strptime(next_date, "%Y-%m-%d").date()
            days_left = (nd - (today or date.today())).days
            if 0 <= days_left <= window_days:
                if verdict == _PASS_VERDICT:
                    verdict = _WARN_VERDICT
                tags.append("report_window")
                reasons.append(f"财报窗口：{next_date} 法定披露（剩{days_left}天），防披露跳空")
        except ValueError:
            pass

    note = _fundamental_note(fund, tags)
    multiplier = 1.0 if verdict != _WARN_VERDICT else float(cfg.get("warn_risk_multiplier", 0.6))
    return {
        "verdict": verdict,
        "reasons": reasons,
        "tags": tags,
        "risk_multiplier": multiplier,
        "note": note,
    }


def _fundamental_note(fund: Dict[str, Any], tags: Optional[List[str]] = None) -> str:
    """推送用一行基本面摘要（强制带数据口径：报告期级）。"""
    parts: List[str] = []
    period = _norm_period(fund.get("report_period") or "")
    profit_yoy = _to_float(fund.get("profit_yoy"))
    deducted_yoy = _to_float(fund.get("deducted_yoy"))
    forecast_type = str(fund.get("forecast_type") or "")
    if profit_yoy is not None:
        seg = f"净利{profit_yoy:+.1f}%"
        if deducted_yoy is not None:
            seg += f"/扣非{deducted_yoy:+.1f}%"
        parts.append(seg)
    if forecast_type:
        seg = f"预告:{forecast_type}"
        if fund.get("forecast_change_pct") is not None:
            seg += f"{fund.get('forecast_change_pct')}%"
        parts.append(seg)
    if tags:
        label = {"earnings_bomb": "业绩雷", "low_quality_growth": "盈利质量低",
                 "report_window": "财报窗口"}.get
        hit = [label(t, t) for t in tags if t in ("earnings_bomb", "low_quality_growth", "report_window")]
        if hit:
            parts.append("(" + "/".join(hit) + ")")
    if period:
        parts.append(f"报告期{period}")
    return " | ".join(parts) or "基本面:N/A"


def reset_fundamental_state() -> None:
    """清空 session 缓存（测试用）。"""
    with _lock:
        _snapshot_cache.clear()
        _snapshot_cache_ts.clear()
        _table_cache.clear()
        _table_cache_ts.clear()
