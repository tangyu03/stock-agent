"""
B4 S3 预期验证（bankuai.md v2，S系列 Week 1 可上线）

博主"预期落空即信号"的工程化：模式判定为 attack（进攻）但主线板块实际走弱
（中位数涨幅 < 0）且全市场涨跌比 < 阈值，连续 N 日触发 → 次日盘前由
market_mode_adaptive 强制降级一档。

零个股映射需求：行业一览快照已给每行业涨跌家数，可聚合出全市场涨跌比。

Week 1 主线代理（B5 未建，已登记为推断参数 GOVERNANCE #26）：
  主线 = 行业快照按当日成交额 Top K（"资金所在即主线"的朴素代理）。
  B5 上线后用 mainline 判定（rs_10前3 ∩ 虹吸前3 ∩ stage∈{lead,confirm}）替换，
  本模块接口不变，仅调用方换数据源。

数据流（两跳，跨日）：
  盘后 s3_daily_eval → record(当日, 触发?) → 更新计数器（data_cache 表）
  次日盘前 market_mode_adaptive._assess_market → 读计数器 → 连续达标降一档（幂等）
"""
import json
import logging
from typing import Dict, List, Optional

import pandas as pd

from src.loop.data_freshness import find_recent_trading_day

logger = logging.getLogger(__name__)

# 模式等级（降级用），与 external_market.apply_external_downgrade 一致
MODE_ORDER = ["retreat", "defend", "attack"]

# 行业一览快照列名（s1_daily_snapshot 实测，2026-08-15）
COL_SECTOR = "板块"
COL_CHG = "涨跌幅"
COL_AMOUNT = "总成交额"
COL_UP = "上涨家数"
COL_DOWN = "下跌家数"


def mainline_proxy(industry_rows: List[Dict], k: int = 5,
                   by: str = COL_AMOUNT) -> List[str]:
    """Week-1 主线代理：当日成交额 Top K 行业。B5 上线后替换调用方。"""
    df = _as_df(industry_rows)
    if df is None or by not in df.columns:
        return []
    return df.sort_values(by, ascending=False).head(k)[COL_SECTOR].tolist()


def compute_indicators(industry_rows: List[Dict],
                       mainline_names: List[str]) -> Optional[Dict]:
    """从行业快照聚合 S3 指标：主线中位数涨幅 + 全市场涨跌比。"""
    df = _as_df(industry_rows)
    if df is None or not mainline_names:
        return None

    # 全市场涨跌比 = Σ上涨家数 / Σ下跌家数（行业一览聚合）
    up = float(df[COL_UP].astype(float).sum())
    down = float(df[COL_DOWN].astype(float).sum())
    ad_ratio = (up / down) if down > 0 else float("inf")

    # 主线板块中位数涨幅
    sub = df[df[COL_SECTOR].isin(mainline_names)]
    if sub.empty:
        return None
    median_chg = float(sub[COL_CHG].astype(float).median())

    return {"mainline_median_chg": median_chg, "ad_ratio": ad_ratio}


def triggered(indicators: Optional[Dict], mode: str,
              median_chg_threshold: float = 0.0,
              ad_ratio_threshold: float = 0.8) -> bool:
    """S3 触发条件：mode==attack 且 主线中位数涨幅<阈值 且 涨跌比<阈值。"""
    if indicators is None or mode != "attack":
        return False
    return (indicators["mainline_median_chg"] < median_chg_threshold
            and indicators["ad_ratio"] < ad_ratio_threshold)


def downgrade_one_notch(mode: str) -> str:
    """降一档：attack→defend→retreat（retreat 不动）。"""
    if mode not in MODE_ORDER:
        return mode
    idx = MODE_ORDER.index(mode)
    return MODE_ORDER[max(0, idx - 1)]


class DivergenceCounter:
    """S3 连续触发计数器，持久化在 data_cache 表（跨进程/跨日）。

    状态：
      last_trigger_date  最近一次触发日（用于"是否连续交易日"判定）
      consecutive_days   连续触发天数
      consumed_date      S3 降级已生效日（幂等：同一天不重复降级）
    """

    KEY = "s3_divergence_counter"

    def __init__(self, consecutive_threshold: int = 2):
        self.last_trigger_date: Optional[str] = None
        self.consecutive_days: int = 0
        self.consumed_date: Optional[str] = None
        self.consecutive_threshold = consecutive_threshold

    # ---------------- 持久化 ----------------
    @classmethod
    def load(cls, consecutive_threshold: Optional[int] = None) -> "DivergenceCounter":
        c = cls(consecutive_threshold or 2)
        try:
            from src.db import get_connection
            conn = get_connection()
            row = conn.execute(
                "SELECT cache_value FROM data_cache WHERE cache_key=?", (cls.KEY,)
            ).fetchone()
            if row:
                d = json.loads(row[0])
                c.last_trigger_date = d.get("last_trigger_date")
                c.consecutive_days = int(d.get("consecutive_days", 0))
                c.consumed_date = d.get("consumed_date")
        except Exception as e:
            logger.warning("S3 计数器读取失败: %s", e)
        return c

    def save(self) -> None:
        try:
            from src.db import get_connection
            conn = get_connection()
            conn.execute(
                "INSERT INTO data_cache(cache_key, cache_value, expire_at) VALUES(?,?,NULL) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "cache_value=excluded.cache_value, expire_at=NULL",
                (self.KEY, json.dumps({
                    "last_trigger_date": self.last_trigger_date,
                    "consecutive_days": self.consecutive_days,
                    "consumed_date": self.consumed_date,
                })),
            )
            conn.commit()
        except Exception as e:
            logger.warning("S3 计数器写入失败: %s", e)

    # ---------------- 盘后记录 ----------------
    def record(self, date: str, was_triggered: bool) -> None:
        """盘后调用：按当日触发与否更新连续天数。"""
        if was_triggered:
            prev = self.last_trigger_date
            # 连续 = 今天是昨交易日的下一个交易日
            is_consecutive = bool(prev) and find_recent_trading_day(date, skip_today=True) == prev
            self.consecutive_days = self.consecutive_days + 1 if is_consecutive else 1
            self.last_trigger_date = date
        else:
            self.consecutive_days = 0
            self.last_trigger_date = None

    # ---------------- 盘前消费 ----------------
    def should_downgrade(self, today: str) -> bool:
        """连续达标且今天尚未消费过 → 应降级（幂等）。"""
        if self.consecutive_days < self.consecutive_threshold:
            return False
        return self.consumed_date != today

    def consume(self, today: str) -> None:
        """标记降级已在本交易日生效，防同日内重复降档。"""
        self.consumed_date = today
        self.save()


def _as_df(rows: List[Dict]) -> Optional[pd.DataFrame]:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    return df if not df.empty else None
