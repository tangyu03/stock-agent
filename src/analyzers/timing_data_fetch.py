"""Data fetching methods for TimingEngine (mixin class)."""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from ..data_layer.stock_data import _volume_share_factor
from ..data_layer.akshare_adapter import get_akshare_adapter
from ..data_layer.skill_wrapper import get_skill_wrapper


logger = logging.getLogger(__name__)


class TimingDataFetchMixin:
    """Data fetching mixin for TimingEngine."""
    def _get_realtime_price(self, stock_code: str) -> Optional[Dict]:
        """获取实时价"""
        # 回测模式：从注入的 K 线取最后收盘价
        if self._backtest_mode:
            kline = self._backtest_kline.get(stock_code, [])
            if kline and len(kline) >= 2:
                last = kline[-1]
                prev = kline[-2]
                # 修复 BUG-B2: 兼容中文键
                close = float(last.get("收盘", last.get("close", 0)) or 0)
                prev_close = float(prev.get("收盘", prev.get("close", 0)) or 0)
                change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                return {"price": close, "change_pct": change_pct}
            return None
        # 实盘模式
        try:
            from ..data_layer.stock_data import batch_get_realtime_quotes
            fresh = batch_get_realtime_quotes([stock_code])
            if stock_code in fresh and fresh.get(stock_code):
                q = fresh[stock_code]
                return {"price": q.get("current_price", 0), "change_pct": q.get("change_pct", 0),
                        "volume_ratio": q.get("volume_ratio", 0), "quote": q}
        except Exception as e:
            logger.debug("非关键异常: %s", e)
        return None

    @staticmethod
    def _sync_last_kline_with_realtime(kline: List[Dict], quote: Optional[Dict]) -> List[Dict]:
        """把当前交易日的实时 OHLCV 合入历史K线，避免现价与指标数据源错位。"""
        if not kline or not quote:
            return kline

        current = float(quote.get("current_price", 0) or 0)
        if current <= 0:
            return kline

        stamp = str(quote.get("timestamp", "") or "")
        if stamp.isdigit() and len(stamp) >= 8:
            quote_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        else:
            quote_date = datetime.now().strftime("%Y-%m-%d")

        last = kline[-1]
        last_date = str(last.get("date", last.get("日期", "")))[:10]
        open_price = float(quote.get("today_open", 0) or 0)
        high_price = float(quote.get("today_high", 0) or 0)
        low_price = float(quote.get("today_low", 0) or 0)
        quote_volume = float(quote.get("volume", 0) or 0)
        quote_amount = float(quote.get("amount", 0) or 0)
        quote_reference_price = float(quote.get("prev_close", 0) or current)
        quote_volume = quote_volume * _volume_share_factor(
            quote_volume, quote_amount, quote_reference_price
        )

        # Historical sources may report lots while realtime volume is shares.
        # Convert every historical bar to shares before adding the intraday bar.
        reference = kline[-2] if len(kline) >= 2 else kline[-1]
        history_factor = 1
        for item in reversed(kline[:-1]):
            reference_volume = float(item.get("volume", item.get("成交量", 0)) or 0)
            reference_amount = float(item.get("amount", item.get("成交额", 0)) or 0)
            reference_close = float(item.get("close", item.get("收盘", 0)) or 0)
            history_factor = _volume_share_factor(
                reference_volume, reference_amount, reference_close
            )
            if reference_volume > 0 and reference_amount > 0:
                break
        if history_factor == 100:
            for item in kline:
                historical_volume = float(item.get("volume", item.get("成交量", 0)) or 0)
                item["volume"] = historical_volume * 100
                item["成交量"] = historical_volume * 100

        volume = quote_volume
        reference_close = float(reference.get("close", reference.get("收盘", 0)) or 0)

        if last_date == quote_date:
            updates = {"close": current, "收盘": current}
            if open_price > 0:
                updates.update({"open": open_price, "开盘": open_price})
            if high_price > 0:
                high_price = max(high_price, current)
                updates.update({"high": high_price, "最高": high_price})
            if low_price > 0:
                low_price = min(low_price, current)
                updates.update({"low": low_price, "最低": low_price})
            if volume > 0:
                updates.update({"volume": volume, "成交量": volume})
            turnover = float(quote.get("turnover_rate", 0) or 0)
            if turnover > 0:
                updates.update({"turnover_rate": turnover, "换手率": turnover})
            last.update(updates)
        elif last_date and last_date < quote_date and open_price > 0 and volume > 0:
            bar = {
                "date": quote_date, "开盘": open_price,
                "high": max(high_price, current), "最高": max(high_price, current),
                "low": min(low_price, current), "最低": min(low_price, current),
                "close": current, "收盘": current,
                "volume": volume, "成交量": volume,
            }
            if open_price > 0:
                bar["open"] = open_price
            turnover = float(quote.get("turnover_rate", 0) or 0)
            if turnover > 0:
                bar.update({"turnover_rate": turnover, "换手率": turnover})
            kline.append(bar)

        return kline

    def _fetch_tech_data(self, stock_code: str, market_mode: str = "defend") -> Dict[str, Any]:
        """获取技术分析数据（含多级均线 + 盘中实时信号）"""
        data = {}

        # 从预取缓存获取实时价
        realtime = self._get_realtime_price(stock_code)
        if realtime:
            data["current_price"] = realtime.get("price", 0)
            data["change_pct"] = realtime.get("change_pct", 0)
            if realtime.get("quote", {}).get("turnover_rate"):
                data["turnover_rate"] = realtime["quote"]["turnover_rate"]
            # 【三】今日开盘价（事件诞生收阳判定用）
            if realtime.get("quote", {}).get("today_open"):
                data["today_open"] = realtime["quote"]["today_open"]
            # 量比：优先用行情接口返回字段（与同花顺/腾讯一致），
            # 不用 K 线均量近似值；无接口数据（回测/停牌/接口缺失）时才用 K 线兜底
            if realtime.get("volume_ratio"):
                data["volume_ratio"] = realtime["volume_ratio"]
                data["volume_ratio_source"] = "行情接口"
                data["volume_ratio_raw"] = realtime["volume_ratio"]
            quote = realtime.get("quote") or {}
            stamp = str(quote.get("timestamp", "") or "")
            if stamp.isdigit() and len(stamp) >= 14:
                data["volume_ratio_sample_time"] = stamp
                data["data_date"] = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
            if quote.get("outer_volume") is not None:
                data["outer_volume"] = quote["outer_volume"]
            if quote.get("inner_volume") is not None:
                data["inner_volume"] = quote["inner_volume"]

        # 获取 K 线
        kline = None
        if self._backtest_mode:
            # 回测模式：从注入的 K 线取
            kline = self._backtest_kline.get(stock_code, [])
        else:
            # 实盘模式：优先缓存，否则拉 akshare
            cached_kline = self._tech_cache.get(stock_code)
            if cached_kline is not None:
                kline = cached_kline
            else:
                hist_calendar_days = self._cfg("prefetch", "hist_calendar_days", default=240)
                start_date = (datetime.now() - timedelta(days=hist_calendar_days)).strftime("%Y%m%d")
                hist_result = self._akshare.get_stock_hist(stock_code, start_date=start_date)
                if hist_result.success and hist_result.data:
                    kline = hist_result.data

        if not self._backtest_mode and kline:
            kline = self._sync_last_kline_with_realtime(kline, realtime.get("quote") if realtime else None)
            last_bar_date = str(kline[-1].get("date", kline[-1].get("日期", "")))[:10]
            data["kline_includes_today"] = bool(
                data.get("data_date") and last_bar_date == data["data_date"]
            )

        if kline:
            data["kline"] = kline
            last_turnover = kline[-1].get("turnover_rate", kline[-1].get("换手率"))
            if data.get("turnover_rate") is None and last_turnover is not None:
                data["turnover_rate"] = last_turnover
            min_klines = self._cfg("tech_data", "min_klines_for_indicator", default=20)
            if kline and len(kline) >= min_klines:
                try:
                    closes = [float(k.get("收盘", k.get("close", 0))) for k in kline]
                    highs = [float(k.get("最高", k.get("high", 0))) for k in kline]
                    lows = [float(k.get("最低", k.get("low", 0))) for k in kline]
                    opens = [float(k.get("开盘", k.get("open", 0))) for k in kline]
                    volumes = [float(k.get("成交量", k.get("volume", 0))) for k in kline]

                    # 修复 BUG-B1: change_pct=0 时也需重新计算（原逻辑只检查 not in）
                    if "current_price" not in data or not data["current_price"]:
                        data["current_price"] = closes[-1]
                    if not data.get("change_pct"):
                        data["change_pct"] = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 and closes[-2] > 0 else 0
                    data["ma5"] = sum(closes[-5:]) / 5
                    data["ma10"] = sum(closes[-10:]) / 10
                    data["ma20"] = sum(closes[-20:]) / 20
                    # MA5 前值（昨日/前日 MA5，用于判断 MA5 是否上升）
                    if len(closes) >= 6:
                        data["ma5_prev"] = sum(closes[-6:-1]) / 5   # 昨日 MA5
                    if len(closes) >= 7:
                        data["ma5_prev2"] = sum(closes[-7:-2]) / 5  # 前日 MA5

                    ma25_window = self._cfg("tech_data", "ma25_window", default=25)
                    ma25_prev_window = self._cfg("tech_data", "ma25_prev_window", default=26)
                    if len(closes) >= ma25_window:
                        data["ma25"] = sum(closes[-ma25_window:]) / ma25_window
                        data["ma25_prev"] = sum(closes[-ma25_prev_window:-1]) / ma25_window if len(closes) >= ma25_prev_window else data["ma25"]
                        data["prev_close"] = closes[-2] if len(closes) >= 2 else closes[-1]

                    ma60_window = self._cfg("tech_data", "ma60_window", default=60)
                    if len(closes) >= ma60_window:
                        data["ma60"] = sum(closes[-ma60_window:]) / ma60_window

                    ma120_window = self._cfg("tech_data", "ma120_window", default=120)
                    if len(closes) >= ma120_window:
                        data["ma120"] = sum(closes[-ma120_window:]) / ma120_window

                    vol_ma60_window = self._cfg("tech_data", "volume_ma60_window", default=60)
                    if len(volumes) >= vol_ma60_window:
                        # 盘中最后一根已是今日累计量，60日基准必须剔除今日，否则放量自我稀释。
                        if data.get("kline_includes_today") and len(volumes) > vol_ma60_window:
                            data["volume_ma60"] = (
                                sum(volumes[-(vol_ma60_window + 1):-1]) / vol_ma60_window
                            )
                        else:
                            data["volume_ma60"] = sum(volumes[-vol_ma60_window:]) / vol_ma60_window
                        data["today_volume"] = volumes[-1]

                    extreme_window = self._cfg("tech_data", "recent_extreme_window", default=20)
                    data["prev_low"] = min(lows[-extreme_window:]) if len(lows) >= extreme_window else (lows[-1] if lows else None)
                    # 【Phase5 回炉】prev_high 语义修正：真昨日高点（旧实现是当日高点，名不副实）。
                    # calculate_stop_loss 的 resistance 用途由 recent_high 覆盖，不受影响。
                    data["prev_high"] = highs[-2] if len(highs) >= 2 else None
                    data["recent_high"] = max(highs[-extreme_window:]) if len(highs) >= extreme_window else max(highs)
                    # 【Phase5 回炉】prior_high：近 N 日高，剔除当日——“创新高/突破”判定的
                    # 正确锚。验收实证（9/8 蘅东光）：盘中 555.10 创历史新高、收盘 549.50
                    # 回落 1.0%，旧口径（含当日高点）判 current≥recent_high×0.99
                    # 即 549.50≥549.55 差 0.05 元失败——“创新高”被错误实现成
                    # “收盘接近日内最高”，在它最该放行的标的上失效。
                    # prior_high=462.30（9/4 涨停价）时 549.50 稳过。
                    data["prior_high"] = self._compute_prior_high(highs, extreme_window)

                    # 收盘量比（当日量/前5日均量）：历史K线无"量比"字段，但收盘时标准量比数学上
                    # 就等于当日量/前5日均量（实测 300843: 0.9325 vs 实时量比 0.93，误差可忽略）
                    vol_ratio_window = self._cfg("tech_data", "volume_ratio_avg_window", default=6)
                    if "volume_ratio" not in data and len(volumes) >= vol_ratio_window:
                        avg_vol = sum(volumes[-vol_ratio_window:-1]) / (vol_ratio_window - 1)
                        data["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

                    # 缩量回踩MA5/MA10（设计问题5: 放宽条件）
                    # 原条件：量比<0.8 + 乖离±1% + 今日下跌 → 几乎不触发
                    # 新条件：量比<1.0（允许量平）+ 乖离±3%（接近均线即可）+ 今日下跌
                    shrink_ratio = self._cfg("tech_data", "shrinking_volume_ratio", default=1.0)
                    vol_ratio = data.get("volume_ratio", 1.0)
                    ma5_val = data.get("ma5", 0)
                    ma10_val = data.get("ma10", 0)
                    if vol_ratio < shrink_ratio:
                        pb5_tol = self._cfg("tech_data", "pullback_ma5_bias_tolerance", default=0.03)
                        if ma5_val > 0 and ma10_val > 0 and ma5_val > ma10_val:
                            bias5 = (closes[-1] - ma5_val) / ma5_val if ma5_val > 0 else 0
                            if -pb5_tol <= bias5 <= pb5_tol and closes[-1] < closes[-2]:
                                data["shrinking_pullback_ma5"] = True
                        pb10_tol = self._cfg("tech_data", "pullback_ma10_bias_tolerance", default=0.03)
                        if ma10_val > 0 and len(closes) >= 11:
                            ma10_prev = sum(closes[-11:-1]) / 10
                            if ma10_val > ma10_prev:
                                bias10 = (closes[-1] - ma10_val) / ma10_val if ma10_val > 0 else 0
                                if -pb10_tol <= bias10 <= pb10_tol and closes[-1] < closes[-2]:
                                    data["shrinking_pullback_ma10"] = True

                    # 对子底（D3 整改 2026-07-25：补"急跌跳水后"前置）
                    price_str = f"{closes[-1]:.2f}".replace(".", "")
                    is_pair = (price_str[-2:] == "99" or price_str[-2:] == "00" or
                               (len(price_str) >= 2 and price_str[-2] == price_str[-1]))
                    if is_pair:
                        drop_5d = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0
                        drop_1d = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
                        if drop_5d < -0.08 or drop_1d < -0.04:
                            data["pair_bottom"] = True

                    # 锤子线
                    hammer_ratio = self._cfg("tech_data", "hammer_lower_shadow_ratio", default=2)
                    if len(closes) >= 1 and len(opens) >= 1 and len(lows) >= 1:
                        body = abs(closes[-1] - opens[-1])
                        lower_shadow = min(closes[-1], opens[-1]) - lows[-1]
                        if body > 0 and lower_shadow > body * hammer_ratio:
                            data["has_hammer"] = True

                    # 长下影/尾盘拉回（左侧反转结构：暴跌日收盘远离最低 → 承接资金）
                    lshadow_ratio = self._cfg("tech_data", "long_lower_shadow_ratio", default=1.5)
                    if len(closes) >= 1 and len(opens) >= 1 and len(lows) >= 1:
                        body = abs(closes[-1] - opens[-1])
                        lower_shadow = min(closes[-1], opens[-1]) - lows[-1]
                        if (body > 0 and lower_shadow >= body * lshadow_ratio
                                and closes[-1] > 0 and lower_shadow / closes[-1] >= 0.01):
                            data["long_lower_shadow"] = True

                    # 缩量企稳（左侧反转结构：跌幅收窄+量能萎缩 → 抛压衰竭）
                    if len(closes) >= 2 and len(volumes) >= 6:
                        chg_1d = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0
                        vol_5d = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 0
                        if vol_5d > 0 and volumes[-1] / vol_5d < 1.0 and -0.025 <= chg_1d < 0:
                            data["shrinking_stabilize"] = True

                    # 跌停板被撬开（D4 整改 2026-07-25：补巨量封单+翘板资金前置）
                    ld_ratio = self._cfg("tech_data", "limit_down_ratio", default=0.9)
                    ld_touch = self._cfg("tech_data", "limit_down_touch_tolerance", default=1.002)
                    ld_open = self._cfg("tech_data", "limit_down_open_threshold", default=1.02)
                    if len(closes) >= 2 and len(lows) >= 1:
                        limit_down = closes[-2] * ld_ratio
                        if lows[-1] <= limit_down * ld_touch and closes[-1] > limit_down * ld_open:
                            vol_5d_avg = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 0
                            heavy_volume = volumes[-1] > vol_5d_avg * 2 if vol_5d_avg > 0 else False
                            today_open = opens[-1] if opens else closes[-1]
                            body = abs(closes[-1] - today_open)
                            lower_shadow = min(today_open, closes[-1]) - lows[-1]
                            sweep_fund = lower_shadow > body * 2 if body > 0 else lower_shadow > 0
                            if heavy_volume and sweep_fund:
                                data["daily_limit_opened"] = True

                    # 开盘强势
                    if len(closes) >= 2 and len(highs) >= 2:
                        data["opening_strong"] = closes[-1] > highs[-2]

                    if "sector_3d_return" not in data:
                        data["sector_3d_return"] = 0

                except (KeyError, ValueError, IndexError) as e:
                    logger.warning("Tech data parse error for %s: %s", stock_code, e)

        # 大盘数据
        if self._market_cache and "index_daily_drop" in self._market_cache:
            data["index_daily_drop"] = self._market_cache["index_daily_drop"]
        else:
            data["index_daily_drop"] = 0
        data["market_volume_yi"] = (self._market_cache or {}).get("market_volume_yi", 0)
        data["market_volume_20d_avg"] = (self._market_cache or {}).get("market_volume_20d_avg", 0)
        data["advance_decline_ratio"] = (self._market_cache or {}).get("advance_decline_ratio", 1.0)
        data["gem_sci_tech_drop"] = (self._market_cache or {}).get("gem_sci_tech_drop", 0)
        data["index_cum_drop_5d"] = (self._market_cache or {}).get("index_cum_drop_5d", 0)
        data["index_down_days_5d"] = (self._market_cache or {}).get("index_down_days_5d", 0)

        # 技术指标 + K 线形态
        tech_vote_min = self._cfg("tech_data", "min_klines_for_tech_vote", default=30)
        if data.get("kline") and len(data["kline"]) >= tech_vote_min:
            try:
                from ..data_layer.stock_data import calc_tech_indicators, detect_kline_patterns
                kline_data = data["kline"]
                tech = calc_tech_indicators(
                    kline_data,
                    market_mode,
                    volume_ratio=data.get("volume_ratio"),
                    volume_ratio_source=data.get("volume_ratio_source") or "K线5日均量",
                    realtime_quote=data,
                )
                if tech:
                    data["tech_signals"] = tech
                patterns = detect_kline_patterns(kline_data)
                if patterns:
                    data["kline_pattern"] = patterns
            except Exception as e:
                logger.debug("技术指标计算失败 %s: %s", stock_code, e)

        # RS line
        try:
            index_kline_data = self._market_cache.get("index_kline") if self._market_cache else None
            if index_kline_data and data.get("kline"):
                from ..data_layer.stock_data import calc_rs_line
                rs_result = calc_rs_line(data["kline"], index_kline_data)
                data["rs_line"] = rs_result
        except Exception:
            data["rs_line"] = {"rs_uptrend": False, "rs_ma_trend": "计算失败"}

        # 周线MACD过滤
        weekly_macd_up = self._compute_weekly_macd_up(stock_code, data.get("kline", []))
        data["weekly_macd_up"] = weekly_macd_up

        # 机构持仓打分（4 数据源投票，API 失败默认中性，session 缓存 1 小时）
        try:
            from .institutional_scorer import score_institutional_holding
            # 【P0-6】大宗交易第五资金源：生产侧注入真实抓取器；
            # 无龙虎榜无两融的标的（北交所）由该源补位。
            try:
                from ..data_layer.block_trade import fetch_block_trade_stats
                from .institutional_scorer import set_block_trade_fetcher
                set_block_trade_fetcher(fetch_block_trade_stats)
            except Exception:
                pass
            inst_score = score_institutional_holding(
                stock_code,
                turnover_available=bool(data.get("turnover_rate")),
            )
            data["institutional_holding"] = inst_score
        except Exception as e:
            logger.debug("机构持仓打分失败 %s: %s", stock_code, e)
            data["institutional_holding"] = {
                "vote_score": 0, "vote_label": "资金中性",
                "votes": {}, "bullish_count": 0, "bearish_count": 0,
                "neutral_count": 4, "stale": False,
            }

        # 【二】基本面快照（Phase2-A）：业绩快报/预告/扣非 → 业绩雷否决、
        # 盈利质量降级、财报窗口提示。数据源全部失败时保持缺省（闸门放行，
        # 不产生假基本面结论）。报告期级数据，推送时必须带口径展示。
        try:
            from .fundamental_gate import fetch_fundamental_snapshot
            fund_snapshot = fetch_fundamental_snapshot(stock_code, data.get("stock_name", "") or "")
            if fund_snapshot:
                data["fundamental"] = fund_snapshot
        except Exception as e:
            logger.debug("基本面快照获取失败 %s: %s", stock_code, e)

        # 【Phase3】估值透镜：PE/PB/净利绝对值/合同负债/存货/研报共识。
        # 观察卡与信号出厂共用；此处做中性口径评估（供展示），
        # signal_plan.build_execution_plan 按 entry_type 重新评估——
        # 追高型策略（确认追强/价量突破）对 model_loss/估值泡沫更严格。
        # 数据源全部失败时保持缺省（透镜放行，不产生假估值结论）。
        try:
            from .valuation_lens import assemble_valuation_lens, attach_analyst_to_institutional
            flow_vote = None
            if isinstance(data.get("institutional_holding"), dict):
                flow_vote = data["institutional_holding"].get("vote_score")
            lens = assemble_valuation_lens(
                stock_code,
                data.get("stock_name", "") or "",
                fundamental=data.get("fundamental"),
                flow_vote=flow_vote,
                config=self._tc,
            )
            if lens:
                data["valuation_lens"] = lens
                # 分析师共识旁路注入机构投票结果（渲染层双标签呈现，不混票）
                if isinstance(data.get("institutional_holding"), dict):
                    attach_analyst_to_institutional(data["institutional_holding"], lens)
        except Exception as e:
            logger.debug("估值透镜获取失败 %s: %s", stock_code, e)

        return data

    def _compute_weekly_macd_up(self, stock_code: str, daily_kline: List[Dict]) -> bool:
        """
        计算周线 MACD 是否向上（DIF > DEA）

        实盘模式和回测模式都从日 K 线聚合为周 K 线再算。
        修复 BUG-A1: DEA 恒等于 DIF（原算法用单日 DIF 做无效递归）
        修复 BUG-B5: EMA12 平滑系数公式错误
        修复 BUG-B6/B18: 实盘模式也聚合周线（不再仅限回测模式）
        """
        weekly_kline = self._tech_cache_weekly.get(stock_code)
        if weekly_kline is None and daily_kline:
            # 实盘+回测都从日 K 聚合周 K
            weekly_kline = self._aggregate_weekly_from_daily(daily_kline)
            if weekly_kline:
                self._tech_cache_weekly[stock_code] = weekly_kline

        if not weekly_kline:
            return False

        min_weeks = self._cfg("tech_data", "weekly_macd_min_weeks", default=26)
        if len(weekly_kline) < min_weeks:
            return False

        try:
            w_closes = [float(k.get("收盘", k.get("close", 0))) for k in weekly_kline]
            if len(w_closes) < min_weeks:
                return False

            ema12_period = self._cfg("tech_data", "ema12_period", default=12)
            ema26_period = self._cfg("tech_data", "ema26_period", default=26)
            dea_period = 9  # DEA 是 DIF 的 EMA9

            # 标准 EMA 计算：用前 N 个值的 SMA 作为种子，然后逐日更新
            # EMA_N = price * 2/(N+1) + EMA_N_prev * (N-1)/(N+1)
            def calc_ema(values: List[float], period: int) -> List[float]:
                if len(values) < period:
                    return []
                # 种子 = 前 period 个值的 SMA
                seed = sum(values[:period]) / period
                emas = [0.0] * (period - 1) + [seed]
                alpha = 2.0 / (period + 1)
                for i in range(period, len(values)):
                    emas.append(values[i] * alpha + emas[-1] * (1 - alpha))
                return emas

            ema12_list = calc_ema(w_closes, ema12_period)
            ema26_list = calc_ema(w_closes, ema26_period)
            if not ema12_list or not ema26_list:
                return False

            # DIF 序列 = EMA12 - EMA26（对齐到相同长度）
            min_len = min(len(ema12_list), len(ema26_list))
            dif_list = []
            for i in range(1, min_len + 1):
                dif_list.append(ema12_list[-i] - ema26_list[-i])
            dif_list.reverse()

            # DEA = DIF 的 EMA9
            dea_list = calc_ema(dif_list, dea_period)
            if not dea_list:
                return False

            # 最新 DIF > DEA → 周线 MACD 向上
            return dif_list[-1] > dea_list[-1]
        except Exception:
            return False

    @staticmethod
    def _aggregate_weekly_from_daily(daily_kline: List[Dict]) -> List[Dict]:
        """从日 K 线聚合为周 K 线（按自然周聚合，周一为周首）"""
        if not daily_kline:
            return []
        from datetime import timedelta  # 修复 BUG-T2: datetime 已在模块顶层导入，此处重复导入遮蔽同名顶层符号
        weekly = []
        current_week = None
        week_rows = []
        for row in daily_kline:
            try:
                # 修复 BUG-B7: 兼容中文键 "日期"
                d = row.get("date", row.get("日期", ""))
                if not d:
                    continue
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
                # ISO 周一为周首
                week_start = dt - timedelta(days=dt.weekday())
                if current_week is None:
                    current_week = week_start
                    week_rows = [row]
                elif week_start == current_week:
                    week_rows.append(row)
                else:
                    if week_rows:
                        weekly.append(TimingDataFetchMixin._aggregate_week_rows(week_rows))
                    current_week = week_start
                    week_rows = [row]
            except Exception:
                continue
        if week_rows:
            weekly.append(TimingDataFetchMixin._aggregate_week_rows(week_rows))
        return weekly

    @staticmethod
    def _aggregate_week_rows(rows: List[Dict]) -> Dict:
        """聚合一周的日 K 为单根周 K"""
        if not rows:
            return {}
        opens = [float(r.get("开盘", r.get("open", 0)) or 0) for r in rows]
        closes = [float(r.get("收盘", r.get("close", 0)) or 0) for r in rows]
        highs = [float(r.get("最高", r.get("high", 0)) or 0) for r in rows]
        lows = [float(r.get("最低", r.get("low", 0)) or 0) for r in rows]
        vols = [float(r.get("成交量", r.get("volume", 0)) or 0) for r in rows]
        return {
            "date": rows[-1].get("date", ""),
            "open": opens[0] if opens else 0,
            "close": closes[-1] if closes else 0,
            "high": max(highs) if highs else 0,
            "low": min(lows) if lows else 0,
            "volume": sum(vols),
        }

    def _calculate_target_range(self, tech_data: Dict, entry_type: str) -> List[float]:
        """计算止盈目标区间"""
        tr = self._tc.get("target_range", {})
        current = tech_data.get("current_price", 0)
        resistance = tech_data.get("resistance", 0) or tech_data.get("recent_high", 0)

        if entry_type == "恐慌抄底":
            return [round(current * tr.get("panic_bottom_low", 1.08), 2),
                    round(current * tr.get("panic_bottom_high", 1.18), 2)]
        elif entry_type == "套利低吸":
            # C5 市况自适应数值纪律（2026-07-25 整改）
            mode = getattr(self, '_current_market_mode', 'defend')
            if mode == 'attack':
                return [round(current * 0.98, 2), round(current * 1.08, 2)]  # 强势日 +8%
            elif mode == 'retreat':
                return [round(current * 0.98, 2), round(current * 1.02, 2)]  # 撤退不套利
            else:
                return [round(current * 0.98, 2), round(current * 1.03, 2)]  # 震荡市 -2/+3
        elif entry_type == "确认追强":
            return [round(current * tr.get("momentum_chase_low", 1.05), 2),
                    round(current * tr.get("momentum_chase_high", 1.12), 2)]
        elif entry_type == "价量突破":
            return [round(current * tr.get("volume_breakout_low", 1.08), 2),
                    round(current * tr.get("volume_breakout_high", 1.15), 2)]
        else:
            if resistance > current:
                return [round(resistance * tr.get("default_with_resistance_low", 0.97), 2),
                        round(resistance * tr.get("default_with_resistance_high", 1.02), 2)]
            return [round(current * tr.get("default_no_resistance_low", 1.05), 2),
                    round(current * tr.get("default_no_resistance_high", 1.08), 2)]

