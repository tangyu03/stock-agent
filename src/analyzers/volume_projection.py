"""
量能外推口径（决策记录 P0-1）— 盘中累计量 → 外推全天量
=====================================================

依据：
  蘅东光 9/7 的 1.02x 拦截是直接实证——盘中累计量对比全天均量，
  午前结构性小于 1 是数学必然（分子只走了半天），与标的强弱无关；
  外推口径下同一数据为 2.0x（1.02 ÷ 午前占比0.51）。A 股日内 U 型
  分布是公共知识，分母查表成本为零。

  外推全天量 = 累计量 ÷ U型分位（当前时刻已成交的日内占比）

反方对冲（已内置）：
  - 10:00 前禁用窗口：开盘冲量时段外推会高估全天量，制造假放量；
  - 封板状态禁用：涨停惜售/跌停恐慌下量能分布失真，外推无意义；
  - 14:45 后退化为累计量：已接近全天量，外推无增益；
  - 回测/日频数据禁用：K 线口径本身就是全天量，外推是口径错配。

作废条件（决策记录原文）：
  若两周误差记录显示 10:30 后外推误差持续 > 15%，说明标的池日内
  分布显著偏离 U 型——此时不是撤销外推，而是把通用查表换成标的池
  自身分位数表（外推框架保留，参数重校）。误差记录表
  （volume_projection_log）就是这条作废条件的数据基础设施。

验证：
  每日收盘比对"外推全天量 vs 实际全天量"，误差进记录表；
  蘅东光 9/7 类形态（1.02x@11:27）回放应从拦截变为触发（外推 ≈2.0x）。
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# A 股日内 U 型累计分布（时刻 → 已成交占全天比例）
# 默认表：开盘 30 分钟 ≈22%、午前合计 ≈52%、尾盘 30 分钟 ≈13%。
# 可由 config/timing.yaml volume_projection.u_curve 整体覆盖
# （作废条件触发时换标的池自身分位数表，框架不动，只换这张表）。
# ============================================================
DEFAULT_U_CURVE: Dict[str, float] = {
    "09:30": 0.00,
    "10:00": 0.22,
    "10:30": 0.31,
    "11:00": 0.39,
    "11:30": 0.52,   # 上午收盘（午前占比≈52%，与"半天分子"实证一致）
    "13:00": 0.52,
    "13:30": 0.60,
    "14:00": 0.68,
    "14:30": 0.79,
    "14:45": 0.87,
    "15:00": 1.00,
}

DEFAULT_CONFIG = {
    "enabled": True,
    "start_time": "10:00",          # 10:00 前禁用（开盘冲量高估全天量）
    "end_time": "14:45",            # 14:45 后退化累计量
    "breakout_threshold": 1.2,      # 外推口径下的量能突破阈值（比实际口径 1.0 更严）
    "error_alert_pct": 15.0,        # 作废条件：10:30 后误差持续 >15%
    "error_check_days": 10,         # 误差统计窗口（两个交易周）
}


def _parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        hour, minute = str(text).split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def _u_curve(config: Optional[Dict]) -> Dict[str, float]:
    curve = dict(DEFAULT_U_CURVE)
    if config and isinstance(config.get("u_curve"), dict) and config["u_curve"]:
        curve = {str(k): float(v) for k, v in config["u_curve"].items()}
    return curve


@dataclass
class ProjectionResult:
    """一次外推的计算结果（含禁用原因，供审计展示）"""
    projected_ratio: Optional[float]     # 外推口径 量/60日均量（None=不可外推）
    u_fraction: Optional[float]          # 当前时刻的 U 型分位
    raw_ratio: Optional[float]           # 实际累计口径 量/60日均量
    mode: str = "ok"                     # ok / disabled / pre_window / post_window /
                                         # limit_locked / non_trading / config_off
    note: str = ""


def u_fraction_at(now: Optional[datetime] = None, config: Optional[Dict] = None) -> Optional[float]:
    """当前时刻的日内累计占比（线性内插）。

    非交易时段（盘前 <9:30 / 盘后 >=15:00 / 周末）返回 1.0——
    此时今日量字段即最近一个完整交易日的全天量（收盘后口径）。
    午休（11:30~13:00）按上午收盘分位（≈0.52）处理。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 1.0
    minutes = now.hour * 60 + now.minute
    curve = _u_curve(config)
    points = sorted(
        (int(k.split(":")[0]) * 60 + int(k.split(":")[1]), float(v))
        for k, v in curve.items()
    )
    if minutes >= points[-1][0] or minutes < points[0][0]:
        return 1.0      # 盘后或盘前：数据口径已是全天量
    for (m0, f0), (m1, f1) in zip(points, points[1:]):
        if m0 <= minutes <= m1:
            if m1 == m0:
                return f1
            weight = (minutes - m0) / (m1 - m0)
            return f0 + (f1 - f0) * weight
    return None


def trading_minute_fraction_at(now: Optional[datetime] = None) -> Optional[float]:
    """当前累计交易分钟占全天 240 分钟的比例（线性，不含 U 型假设）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 1.0
    current = now.hour * 60 + now.minute
    if current < 9 * 60 + 30:
        return 0.0
    if current < 11 * 60 + 30:
        return (current - 9 * 60 - 30) / 240.0
    if current < 13 * 60:
        return 120.0 / 240.0
    if current < 15 * 60:
        return (120.0 + current - 13 * 60) / 240.0
    return 1.0


def parse_sample_time(sample_time) -> Optional[datetime]:
    """解析量比采样时间；HH:MM 固定映射到周一，避免周末换算成收盘口径。"""
    if isinstance(sample_time, datetime):
        return sample_time
    text = str(sample_time or "").strip()
    if not text:
        return None
    try:
        if text.isdigit() and len(text) >= 14:
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S")
        if ":" in text:
            hh, mm = text.split(":")[:2]
            return datetime(2000, 1, 3, int(hh), int(mm), second=0, microsecond=0)
    except (ValueError, TypeError):
        return None
    return None


def same_period_ratio_from_interface(
    interface_ratio: Optional[float],
    sample_time=None,
    config: Optional[Dict] = None,
) -> Optional[float]:
    """把接口量比换算为同期累计量比。

    接口量比 = 当日分钟均量 / 过去5日全天分钟均量。要和过去5日同时点
    累计量对齐，用 U 型分位替换线性时间分位：
      同期量比 = 接口量比 × (已开市分钟/240) ÷ U型分位
    缺时间分位或 U 型分位时返回 None，不用接口量比冒充同期口径。
    """
    ratio = float(interface_ratio) if interface_ratio is not None else None
    if ratio is None or ratio <= 0:
        return None
    sample_dt = parse_sample_time(sample_time)
    if sample_dt is None:
        return None

    linear = trading_minute_fraction_at(sample_dt)
    u_fraction = u_fraction_at(sample_dt, config)
    if linear is None or linear <= 0 or u_fraction is None or u_fraction <= 0:
        return None
    return round(ratio * linear / u_fraction, 3)


def project_volume_ratio(
    raw_ratio: Optional[float],
    change_pct: Optional[float],
    now: Optional[datetime] = None,
    config: Optional[Dict] = None,
    limit_ratio: float = 0.10,
) -> ProjectionResult:
    """把"盘中累计量/60日均量"外推为"全天量/60日均量"。

    Args:
        raw_ratio: 累计口径 量/60日均量（VolumeSnapshot.volume_vs_ma60）
        change_pct: 当日涨跌幅（%），封板判定用
        limit_ratio: 涨跌停幅度（主板 0.10 / 创科 0.20 / 北交 0.30）
        config: volume_projection 配置块（缺省用 DEFAULT_CONFIG）
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if isinstance(v, (int, float, str, bool))})

    now = now or datetime.now()
    result = ProjectionResult(projected_ratio=None, u_fraction=None, raw_ratio=raw_ratio)

    if not cfg.get("enabled", True):
        result.mode, result.note = "config_off", "外推开关关闭"
        return result
    if raw_ratio is None or raw_ratio <= 0:
        result.mode, result.note = "disabled", "累计量数据缺失"
        return result

    # 封板禁用：涨跌停附近量能分布失真（惜售/恐慌），外推无意义
    if change_pct is not None and limit_ratio > 0:
        if abs(float(change_pct)) / 100.0 >= limit_ratio * 0.995:
            result.mode, result.note = "limit_locked", f"封板状态不外推(涨跌幅{change_pct:.1f}%)"
            return result

    start = _parse_hhmm(str(cfg.get("start_time", "10:00")), dtime(10, 0))
    end = _parse_hhmm(str(cfg.get("end_time", "14:45")), dtime(14, 45))
    current = dtime(now.hour, now.minute)

    if now.weekday() >= 5 or current >= dtime(15, 0) or current < dtime(9, 30):
        # 非交易时段：累计量即全天量（日频/收盘后口径）
        result.u_fraction = 1.0
        result.projected_ratio = raw_ratio
        result.mode, result.note = "non_trading", "收盘口径=全天量"
        return result

    if current < start:
        result.mode, result.note = "pre_window", "10:00前禁用外推(开盘冲量会高估全天量)"
        return result
    if current > end:
        result.u_fraction = u_fraction_at(now, config)
        result.projected_ratio = raw_ratio
        result.mode, result.note = "post_window", "14:45后退化累计量"
        return result

    fraction = u_fraction_at(now, config)
    if fraction is None or fraction <= 0:
        result.mode, result.note = "disabled", "U型分位缺失"
        return result
    result.u_fraction = round(fraction, 4)
    result.projected_ratio = round(raw_ratio / fraction, 3)
    result.mode = "ok"
    result.note = f"外推口径{result.projected_ratio:.2f}x(累计{raw_ratio:.2f}x÷U分位{fraction:.2f})"
    return result


# ============================================================
# 误差记录表（作废条件的数据基础设施）
# ============================================================

_ensure_log_table_sql = """
CREATE TABLE IF NOT EXISTS volume_projection_log (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    sample_time TEXT,
    raw_ratio REAL,
    projected_ratio REAL,
    actual_ratio REAL,
    error_pct REAL,
    updated_at TEXT,
    PRIMARY KEY (trade_date, stock_code)
)
"""


class InMemoryProjectionStore:
    """内存误差记录（单测用）"""

    def __init__(self):
        self.rows: Dict[tuple, Dict] = {}

    def upsert(self, row: Dict) -> None:
        key = (row.get("trade_date", ""), row.get("stock_code", ""))
        self.rows[key] = row

    def fetch_days(self, days: int = 10) -> List[Dict]:
        return list(self.rows.values())

    def update_actual(self, trade_date: str, stock_code: str, actual_ratio: float) -> bool:
        key = (trade_date, stock_code)
        row = self.rows.get(key)
        if not row:
            return False
        row["actual_ratio"] = actual_ratio
        projected = row.get("projected_ratio")
        row["error_pct"] = (
            abs(projected - actual_ratio) / actual_ratio * 100
            if projected and actual_ratio else None
        )
        return True


class DbProjectionStore:
    """SQLite 误差记录（实盘用）"""

    def upsert(self, row: Dict) -> None:
        try:
            from ..db import get_conn
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(_ensure_log_table_sql)
                cursor.execute("""
                    INSERT OR REPLACE INTO volume_projection_log
                    (trade_date, stock_code, stock_name, sample_time, raw_ratio,
                     projected_ratio, actual_ratio, error_pct, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row.get("trade_date"), row.get("stock_code"), row.get("stock_name"),
                    row.get("sample_time"), row.get("raw_ratio"), row.get("projected_ratio"),
                    row.get("actual_ratio"), row.get("error_pct"),
                    datetime.now().isoformat(timespec="seconds"),
                ))
                conn.commit()
        except Exception as e:
            logger.error("外推记录写入失败 %s: %s", row.get("stock_code"), e)

    def fetch_days(self, days: int = 10) -> List[Dict]:
        try:
            from ..db import get_conn
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(_ensure_log_table_sql)
                cursor.execute(
                    "SELECT * FROM volume_projection_log ORDER BY trade_date DESC, stock_code"
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("外推记录读取失败: %s", e)
            return []

    def update_actual(self, trade_date: str, stock_code: str, actual_ratio: float) -> bool:
        try:
            from ..db import get_conn
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(_ensure_log_table_sql)
                cursor.execute(
                    "SELECT projected_ratio FROM volume_projection_log "
                    "WHERE trade_date=? AND stock_code=?",
                    (trade_date, stock_code),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                projected = float(row["projected_ratio"] or 0)
                error = (
                    abs(projected - actual_ratio) / actual_ratio * 100
                    if projected and actual_ratio else None
                )
                cursor.execute(
                    "UPDATE volume_projection_log SET actual_ratio=?, error_pct=? "
                    "WHERE trade_date=? AND stock_code=?",
                    (actual_ratio, error, trade_date, stock_code),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error("外推实际值回填失败 %s: %s", stock_code, e)
            return False


_memory_store: Optional[InMemoryProjectionStore] = None


def set_projection_store(store) -> None:
    """测试注入用：替换默认存储。"""
    global _memory_store
    _memory_store = store


def _store():
    global _memory_store
    if _memory_store is None:
        _memory_store = DbProjectionStore()
    return _memory_store


def record_projection(
    stock_code: str,
    stock_name: str,
    raw_ratio: Optional[float],
    projected_ratio: Optional[float],
    sample_time: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> None:
    """盘中扫描时记录当日外推快照（幂等 upsert，收盘后回填实际值）。"""
    if projected_ratio is None:
        return
    _store().upsert({
        "trade_date": trade_date or date.today().isoformat(),
        "stock_code": stock_code,
        "stock_name": stock_name,
        "sample_time": sample_time or datetime.now().strftime("%H:%M"),
        "raw_ratio": raw_ratio,
        "projected_ratio": projected_ratio,
    })


def finalize_day_projection(actual_ratios: Dict[str, float], trade_date: Optional[str] = None) -> int:
    """收盘后回填实际全天量口径，返回更新行数（误差进记录表）。"""
    trade_date = trade_date or date.today().isoformat()
    updated = 0
    for code, actual in (actual_ratios or {}).items():
        if actual and actual > 0 and _store().update_actual(trade_date, code, float(actual)):
            updated += 1
    return updated


def projection_error_report(days: int = 10, alert_pct: float = 15.0) -> Dict:
    """误差统计（作废条件判定材料）。

    只统计 10:30 之后采样的行（开盘冲量时段天然高估，不作数）。
    返回 {samples, mean_error_pct, max_error_pct, persistent_over_alert, note}
    persistent_over_alert=True → 决策记录的作废条件触发：
    不是撤销外推，而是把通用 U 型表换成标的池自身分位数表（参数重校）。
    """
    rows = _store().fetch_days(days)
    samples = []
    for row in rows:
        sample_time = str(row.get("sample_time") or "")
        error = row.get("error_pct")
        if error is None:
            continue
        try:
            hh, mm = sample_time.split(":")
            if int(hh) * 60 + int(mm) < 10 * 60 + 30:
                continue  # 10:30 前的样本不计入（决策记录口径）
        except ValueError:
            continue
        samples.append(float(error))
    if not samples:
        return {
            "samples": 0,
            "mean_error_pct": None,
            "max_error_pct": None,
            "persistent_over_alert": False,
            "note": "外推误差记录尚无 10:30 后样本，作废条件无从判定（记录表先存在）",
        }
    mean_error = sum(samples) / len(samples)
    max_error = max(samples)
    persistent = mean_error > alert_pct
    note = (
        f"10:30后样本{len(samples)}个，平均误差{mean_error:.1f}%"
        + (
            f"（>{alert_pct:.0f}%持续偏高：按作废条件换标的池自身分位数表，外推框架保留，参数重校）"
            if persistent else f"（未超{alert_pct:.0f}%阈值，U型查表继续服役）"
        )
    )
    return {
        "samples": len(samples),
        "mean_error_pct": round(mean_error, 2),
        "max_error_pct": round(max_error, 2),
        "persistent_over_alert": persistent,
        "note": note,
    }
