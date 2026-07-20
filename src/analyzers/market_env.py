"""
市场环境增强指标

补充主流量化研究关注的市场层面指标：
1. 成交额历史分位（250日）— 放量 vs 缩量
2. 涨跌家数比 — 市场宽度（含备用方案）
3. 沪深300 作为全市场代表（替代上证指数）

这些指标用于：
- market_mode 判定（第 6/7 维度）
- 恐慌抄底条件（放量恐慌才抄）
- 板块退潮辅助判断
"""
import logging
from typing import Dict, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


# 市场环境缓存（当日只算一次）
_market_env_cache: Optional[Dict] = None
_market_env_date: Optional[str] = None


def get_market_environment(force_refresh: bool = False) -> Dict:
    """
    获取市场环境增强指标（带当日缓存）

    Returns:
        {
            "volume_percentile": float,      # 成交量 250 日分位（0-100）
            "volume_label": str,             # 放量/量平/缩量
            "advance_count": int,            # 上涨家数
            "decline_count": int,            # 下跌家数
            "flat_count": int,               # 平盘家数
            "ad_ratio": float,               # 涨跌比（涨/跌）
            "breadth_label": str,            # 极强/强势/弱势/极弱
            "limit_up_count": int,           # 涨停数
            "limit_down_count": int,         # 跌停数
            "csi300_close": float,           # 沪深300收盘价
            "csi300_change_pct": float,      # 沪深300涨跌幅%
            "turnover_ratio": float,         # 换手率代理（今日/250日均量）
            "turnover_label": str,           # 高换手/正常/低换手
            "futures_basis": float,          # IM期货基差（期货-现货）
            "futures_basis_pct": float,      # IM基差%
            "futures_basis_annual": float,   # IM年化基差%
            "futures_label": str,            # 升水/贴水/中性
        }
    """
    global _market_env_cache, _market_env_date
    today = datetime.now().strftime("%Y-%m-%d")

    if not force_refresh and _market_env_cache and _market_env_date == today:
        return _market_env_cache

    env = {
        "volume_percentile": 50.0,
        "volume_label": "量平",
        "advance_count": 0,
        "decline_count": 0,
        "flat_count": 0,
        "ad_ratio": 1.0,
        "breadth_label": "未知",
        "limit_up_count": 0,
        "limit_down_count": 0,
        "csi300_close": 0,
        "csi300_change_pct": 0,
        "turnover_ratio": 1.0,
        "turnover_label": "正常",
        "futures_basis": 0.0,
        "futures_basis_pct": 0.0,
        "futures_basis_annual": 0.0,
        "futures_label": "未知",
    }

    try:
        import akshare as ak

        # 1. 沪深300 成交量分位 + 涨跌幅
        try:
            df = ak.stock_zh_index_daily(symbol="sh000300")
            if df is not None and not df.empty:
                vols = df["volume"].astype(float).tolist()
                closes = df["close"].astype(float).tolist()
                today_vol = vols[-1]
                today_close = closes[-1]
                prev_close = closes[-2] if len(closes) >= 2 else today_close

                # 250 日分位
                vol_window = vols[-250:] if len(vols) >= 250 else vols
                vol_window_sorted = sorted(vol_window)
                pct = sum(1 for v in vol_window_sorted if v <= today_vol) / len(vol_window_sorted) * 100
                env["volume_percentile"] = round(pct, 1)
                if pct >= 70:
                    env["volume_label"] = "放量"
                elif pct >= 40:
                    env["volume_label"] = "量平"
                else:
                    env["volume_label"] = "缩量"

                env["csi300_close"] = round(today_close, 2)
                env["csi300_change_pct"] = round((today_close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

                # P1-3: 换手率代理 = 今日成交量 / 250 日均量
                avg_vol_250 = sum(vol_window) / len(vol_window) if vol_window else 1
                env["turnover_ratio"] = round(today_vol / avg_vol_250, 2) if avg_vol_250 > 0 else 1.0
                if env["turnover_ratio"] >= 1.3:
                    env["turnover_label"] = "高换手"
                elif env["turnover_ratio"] >= 0.8:
                    env["turnover_label"] = "正常"
                else:
                    env["turnover_label"] = "低换手"
        except Exception as e:
            logger.warning("沪深300数据拉取失败: %s", e)

        # P1-1: IM 期货基差（中证1000期指 vs 现货）
        # 基差 = 期货收盘 - 现货收盘
        # 升水（基差 > 0）= 做空压力未累积，市场可能企稳
        # 贴水（基差 < 0）= 做空压力大，趋势性下跌信号
        try:
            # 找当月主力合约（IM + 年月）
            from datetime import timedelta
            now = datetime.now()
            # 当月合约代码：IM + YYMM
            contract_month = now.strftime("%y%m")
            contract_code = f"IM{contract_month}"

            df_fut = ak.futures_zh_daily_sina(symbol=contract_code)
            if df_fut is not None and not df_fut.empty:
                fut_close = float(df_fut.iloc[-1]["close"])

                # 中证1000现货
                df_spot = ak.stock_zh_index_daily(symbol="sh000852")
                if df_spot is not None and not df_spot.empty:
                    spot_close = float(df_spot.iloc[-1]["close"])

                    basis = fut_close - spot_close
                    basis_pct = basis / spot_close * 100 if spot_close > 0 else 0

                    # 年化基差（估算到期天数）
                    # 当月合约通常在每月第三个周五交割
                    days_to_expiry = 15  # 简化估算
                    basis_annual = basis_pct / days_to_expiry * 365 if days_to_expiry > 0 else 0

                    env["futures_basis"] = round(basis, 2)
                    env["futures_basis_pct"] = round(basis_pct, 2)
                    env["futures_basis_annual"] = round(basis_annual, 1)

                    if basis_pct > 0.5:
                        env["futures_label"] = "升水(做空压力低)"
                    elif basis_pct < -1.0:
                        env["futures_label"] = "深贴水(做空压力大)"
                    elif basis_pct < -0.3:
                        env["futures_label"] = "贴水(有做空压力)"
                    else:
                        env["futures_label"] = "中性"
        except Exception as e:
            logger.warning("期货基差计算失败: %s", e)

        # 2. 涨跌家数（legu 接口）
        try:
            df = ak.stock_market_activity_legu()
            if df is not None and not df.empty:
                stats = {}
                for _, row in df.iterrows():
                    key = str(row.get("item", ""))
                    raw_val = str(row.get("value", "")).strip()
                    # 去掉 % 和非数字字符
                    try:
                        val = float(raw_val.replace("%", "").replace(",", ""))
                    except (ValueError, TypeError):
                        val = 0
                    stats[key] = val

                env["advance_count"] = int(stats.get("上涨", 0))
                env["decline_count"] = int(stats.get("下跌", 0))
                env["flat_count"] = int(stats.get("平盘", 0))
                env["limit_up_count"] = int(stats.get("涨停", 0))
                env["limit_down_count"] = int(stats.get("跌停", 0))

                if env["decline_count"] > 0:
                    env["ad_ratio"] = round(env["advance_count"] / env["decline_count"], 2)
                else:
                    env["ad_ratio"] = float("inf") if env["advance_count"] > 0 else 1.0

                # 市场宽度标签
                ratio = env["ad_ratio"]
                if ratio >= 2.0:
                    env["breadth_label"] = "极强"
                elif ratio >= 1.2:
                    env["breadth_label"] = "强势"
                elif ratio >= 0.8:
                    env["breadth_label"] = "中性"
                elif ratio >= 0.3:
                    env["breadth_label"] = "弱势"
                else:
                    env["breadth_label"] = "极弱"
        except Exception as e:
            logger.warning("涨跌家数拉取失败: %s", e)
            # 备用：用新浪全A实时行情估算
            try:
                df = ak.stock_zh_a_spot()
                if df is not None and not df.empty:
                    chg_col = "涨跌幅" if "涨跌幅" in df.columns else None
                    if chg_col:
                        chgs = df[chg_col].astype(float)
                        env["advance_count"] = int((chgs > 0).sum())
                        env["decline_count"] = int((chgs < 0).sum())
                        env["flat_count"] = int((chgs == 0).sum())
                        if env["decline_count"] > 0:
                            env["ad_ratio"] = round(env["advance_count"] / env["decline_count"], 2)
                        ratio = env["ad_ratio"]
                        if ratio >= 2.0:
                            env["breadth_label"] = "极强"
                        elif ratio >= 1.2:
                            env["breadth_label"] = "强势"
                        elif ratio >= 0.8:
                            env["breadth_label"] = "中性"
                        elif ratio >= 0.3:
                            env["breadth_label"] = "弱势"
                        else:
                            env["breadth_label"] = "极弱"
                        logger.info("涨跌家数（备用方案）: 涨%d 跌%d", env["advance_count"], env["decline_count"])
            except Exception as e2:
                logger.warning("涨跌家数备用方案也失败: %s", e2)

    except Exception as e:
        logger.warning("市场环境指标获取失败: %s", e)

    _market_env_cache = env
    _market_env_date = today

    logger.info("市场环境: 量分位=%.0f%%(%s), 换手=%.2f(%s), 涨跌比=%.2f(%s), 涨停%d/跌停%d, 沪深300=%s(%s%%), IM基差=%s%%(%s)",
                env["volume_percentile"], env["volume_label"],
                env["turnover_ratio"], env["turnover_label"],
                env["ad_ratio"], env["breadth_label"],
                env["limit_up_count"], env["limit_down_count"],
                env["csi300_close"], env["csi300_change_pct"],
                env["futures_basis_pct"], env["futures_label"])

    return env


def is_volume_surge() -> bool:
    """是否放量（分位 >= 70%）"""
    env = get_market_environment()
    return env["volume_percentile"] >= 70


def is_volume_shrink() -> bool:
    """是否缩量（分位 < 30%）"""
    env = get_market_environment()
    return env["volume_percentile"] < 30


def is_market_extreme_weak() -> bool:
    """市场是否极弱（涨跌比 < 0.2）"""
    env = get_market_environment()
    return 0 < env["ad_ratio"] < 0.2


def is_market_extreme_strong() -> bool:
    """市场是否极强（涨跌比 > 3.0）"""
    env = get_market_environment()
    return env["ad_ratio"] > 3.0


def print_market_env():
    """打印市场环境"""
    env = get_market_environment(force_refresh=True)
    print()
    print("=" * 60)
    print("  市场环境增强指标")
    print("=" * 60)
    print(f"  📊 成交量分位: {env['volume_percentile']:.0f}% ({env['volume_label']})")
    print(f"  🔄 换手率:     {env['turnover_ratio']:.2f}x ({env['turnover_label']})")
    print(f"  📈 涨跌家数:   涨{env['advance_count']} 跌{env['decline_count']} 平{env['flat_count']}")
    print(f"  📊 涨跌比:     {env['ad_ratio']:.2f} ({env['breadth_label']})")
    print(f"  🔥 涨停/跌停:  {env['limit_up_count']} / {env['limit_down_count']}")
    print(f"  📉 沪深300:    {env['csi300_close']} ({env['csi300_change_pct']:+.2f}%)")
    print(f"  📊 IM基差:     {env['futures_basis_pct']:+.2f}% (年化{env['futures_basis_annual']:.1f}%) → {env['futures_label']}")
    print()
    print("  快速判断:")
    if is_volume_surge():
        print("    ⚠️ 放量 — 如是下跌则为恐慌宣泄，可能接近底部")
    elif is_volume_shrink():
        print("    ⚠️ 缩量 — 交投清淡，如下跌则属阴跌，不宜抄底")
    if is_market_extreme_weak():
        print("    🔴 市场极弱 — 涨跌比 < 0.2，恐慌抄底条件满足")
    if is_market_extreme_strong():
        print("    🟢 市场极强 — 涨跌比 > 3.0，注意过热风险")
    if env["futures_basis_pct"] > 0.5:
        print("    📊 期指升水 — 做空压力未累积，市场可能企稳")
    elif env["futures_basis_pct"] < -1.0:
        print("    📊 期指深贴水 — 做空压力大，警惕趋势性下跌")
    print("=" * 60)
