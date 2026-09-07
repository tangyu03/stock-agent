"""
交易日志
记录所有信号和用户操作

【六】记录闭环：每笔交易四行日志——
  1. 假说原文（hypothesis_x/y/z/w + sentence，推送时落库）
  2. 实际出入场（actual_price + exit_price/exit_date，回执联动）
  3. Z/W 是否触发（zw_triggered，卖出回执时自动归类）
  4. 事后归因（review_outcome: logic_right/luck/logic_wrong，用户回执）
"""
import json
import logging
from typing import Dict, Optional, List
from datetime import datetime, date

from ..db import get_connection, get_conn

logger = logging.getLogger(__name__)


class TradeLogger:
    """交易日志记录器"""

    def log_signal(
        self,
        signal_type: str,
        stock_code: str,
        stock_name: str,
        signal_data: Dict,
        user_action: str = "pending",
        actual_price: float = 0,
        actual_position: float = 0,
        note: str = "",
        shares: float = 0,
    ) -> Optional[int]:
        """
        记录信号日志

        Args:
            signal_type: buy / sell / t0_buy / t0_sell
            stock_code: 股票代码
            stock_name: 股票名称
            signal_data: 信号详细数据
            user_action: pending / executed / ignored / modified
            actual_price: 实际成交价
            actual_position: 实际仓位
            note: 备注
            shares: 建议/实际股数（P1-3 反馈闭环，executed 后用于聚合持仓）

        Returns:
            新记录 id（供回执脚本 update_action 定位）；失败返回 None
        """
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                now = datetime.now()
                hyp = signal_data.get("hypothesis") or {}
                cursor.execute(
                    """INSERT INTO trade_logs
                    (date, time, stock_code, stock_name, signal_type, entry_type, exit_type,
                     trigger_price, shares, stop_loss, target_price, suggested_position,
                     mode_at_signal, sector_status, market_score,
                     user_action, actual_price, actual_position, note,
                     hypothesis_x, hypothesis_y, hypothesis_z, hypothesis_w,
                     hypothesis_sentence, paired_z, paired_w_low, paired_w_high,
                     z_reference, event_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now.strftime("%Y-%m-%d"),
                        now.strftime("%H:%M:%S"),
                        stock_code,
                        stock_name,
                        signal_type,
                        signal_data.get("entry_type", ""),
                        signal_data.get("exit_type", ""),
                        signal_data.get("trigger_price", 0),
                        shares,
                        signal_data.get("stop_loss", 0),
                        signal_data.get("target_price", 0),
                        signal_data.get("suggested_position", 0),
                        signal_data.get("mode_at_signal", ""),
                        signal_data.get("sector_status", ""),
                        signal_data.get("market_score", 0),
                        user_action,
                        actual_price,
                        actual_position,
                        note,
                        str(hyp.get("x", "") or signal_data.get("hypothesis_x", "") or ""),
                        str(hyp.get("y", "") or signal_data.get("hypothesis_y", "") or ""),
                        str(hyp.get("z", "") or signal_data.get("hypothesis_z", "") or ""),
                        str(hyp.get("w", "") or signal_data.get("hypothesis_w", "") or ""),
                        str(hyp.get("sentence", "") or signal_data.get("hypothesis_sentence", "") or ""),
                        signal_data.get("paired_z", 0) or hyp.get("z", 0) or 0,
                        signal_data.get("paired_w_low", 0) or (hyp.get("w") or [0])[0] or 0,
                        signal_data.get("paired_w_high", 0) or (hyp.get("w") or [0])[-1] or 0,
                        signal_data.get("z_reference", 0) or hyp.get("z_reference", 0) or 0,
                        signal_data.get("event_id", "") or "",
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error("记录交易日志失败: %s", e)
            return None

    def log_rejection(
        self,
        stock_code: str,
        stock_name: str,
        entry_type: str,
        reasons: List[str],
        detail: Dict,
    ) -> Optional[int]:
        """【一】出厂拒绝留痕：假说不完整的信号不推送，但写入 signal_rejections 可审计。

        reasons: 拒绝原因列表（缺 X/Y/Z/W、倒挂、缓冲不足…）
        detail:  {benchmark_price, stop_loss, target_range, hypothesis}
        """
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                now = datetime.now()
                missing = [
                    label for label, key in (
                        ("X", "x"), ("Y", "y"), ("Z", "z"), ("W", "w"),
                    )
                    if not (detail.get("hypothesis") or {}).get(key)
                ]
                cursor.execute(
                    """INSERT INTO signal_rejections
                    (date, time, stock_code, stock_name, entry_type,
                     missing_fields, reason, detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now.strftime("%Y-%m-%d"),
                        now.strftime("%H:%M:%S"),
                        stock_code,
                        stock_name,
                        entry_type,
                        ",".join(missing),
                        "; ".join(reasons),
                        json.dumps(detail, ensure_ascii=False, default=str),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error("记录拒绝留痕失败: %s", e)
            return None

    def update_action(self, log_id: int, user_action: str, actual_price: float = 0, actual_position: float = 0) -> bool:
        """更新用户操作（回执脚本调用）。

        【六】回执联动：
          - buy executed 且带 event_id → 信号事件转 triggered（受众分流/生命周期）
          - sell executed → 自动回填开仓行的 exit_price/exit_date/pnl_pct/zw_triggered
            （四行日志的第二、三行自动闭环）
        """
        row = self._get_log(log_id)
        # 卖出回执前先锁定当前开仓（更新卖行后持仓即视为平仓，取不到）
        pending_position = None
        if (row and user_action == "executed"
                and row.get("signal_type") in ("sell", "t0_sell")):
            pending_position = self.get_open_position(row.get("stock_code") or "")
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE trade_logs SET user_action = ?, actual_price = ?, actual_position = ? WHERE id = ?",
                    (user_action, actual_price, actual_position, log_id),
                )
                conn.commit()
        except Exception as e:
            logger.error("更新交易日志失败: %s", e)
            return False
        try:
            if not row:
                return True
            if user_action == "executed" and row.get("signal_type") in ("buy", "t0_buy"):
                self._mark_event_triggered(row)
            elif pending_position and user_action == "executed":
                exit_price = actual_price or float(row.get("trigger_price") or 0)
                self._link_exit_to_position(row, exit_price, pending_position)
        except Exception as e:
            logger.error("回执联动失败 #%s: %s", log_id, e)
        return True

    def _get_log(self, log_id: int) -> Optional[Dict]:
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trade_logs WHERE id = ?", (log_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error("读取日志失败 #%s: %s", log_id, e)
            return None

    def _mark_event_triggered(self, row: Dict) -> None:
        """买入回执 → 信号事件转 triggered（后续按配对出场跟踪）"""
        event_id = row.get("event_id") or ""
        if not event_id:
            return
        try:
            from ..analyzers.signal_lifecycle import DbSignalEventStore
            DbSignalEventStore().update_status(event_id, "triggered", "回执成交，转入配对出场跟踪")
        except Exception as e:
            logger.debug("事件转 triggered 失败 %s: %s", event_id, e)

    def _link_exit_to_position(self, sell_row: Dict, exit_price: float, position: Optional[Dict] = None) -> None:
        """卖出回执 → 回填开仓行的离场四行日志（实际出入场/ZW触发/盈亏）。"""
        code = sell_row.get("stock_code") or ""
        if not code:
            return
        if position is None:
            position = self.get_open_position(code)
        if not position:
            return
        buy_id = position.get("log_id")
        entry_price = float(position.get("actual_price") or position.get("trigger_price") or 0)
        if not buy_id or entry_price <= 0 or exit_price <= 0:
            return
        exit_type = str(sell_row.get("exit_type") or "")
        if "策略兑现" in exit_type:
            zw = "W"
        elif "破位止损" in exit_type or "策略认错" in exit_type or "信号作废" in exit_type:
            zw = "Z"
        else:
            zw = "系统"
        pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE trade_logs SET zw_triggered = ?, exit_price = ?,
                       exit_date = ?, pnl_pct = ? WHERE id = ?""",
                    (zw, exit_price, sell_row.get("date") or date.today().isoformat(),
                     pnl_pct, buy_id),
                )
                conn.commit()
                logger.info("回执联动: 买入#%d 已回填 ZW=%s pnl=%.2f%%", buy_id, zw, pnl_pct)
        except Exception as e:
            logger.error("回填离场信息失败 #%s: %s", buy_id, e)

    def get_current_holdings(self) -> List[Dict]:
        """获取当前持仓（P1-3 反馈闭环：优先 trade_logs 已执行记录聚合，无则回退 add_plans）

        口径 1（首选）：trade_logs 中 user_action='executed' 的 buy/t0_buy 累加股数、
            sell/t0_sell 扣减股数；cost_price 取最近一次 executed 买入的 actual_price
            （未回填时用 trigger_price）。净持仓 >0 才返回。
        口径 2（回退）：portfolio.yaml add_plans 中 status='active' 且任一 level.executed=True。
        两口径都为空时返回空列表 —— 调度器按空仓运行，买入上限由
        live_scheduler 的 position_limit 总仓位闸门兜底。
        """
        holdings = self._aggregate_executed_holdings()
        if holdings:
            return holdings
        # 回退：add_plans 已执行计划（legacy 口径）
        try:
            from ..config_models import load_config
            portfolio = load_config("portfolio.yaml")
        except Exception as e:
            logger.error("读取 portfolio.yaml 失败: %s", e)
            return []
        result = []
        for plan in portfolio.get("add_plans", []) or []:
            if not isinstance(plan, dict) or plan.get("status") != "active":
                continue
            levels = plan.get("levels", []) or []
            if not any(lev.get("executed") for lev in levels):
                continue
            code = plan.get("stock_code", "")
            entry_price = plan.get("entry_price", 0) or 0
            if not code or entry_price <= 0:
                continue
            shares = int((250_000 / entry_price) // 100) * 100
            result.append({
                "code": code,
                "stock_name": plan.get("stock_name", code),
                "shares": shares,
                "cost_price": entry_price,
            })
        return result

    def _aggregate_executed_holdings(self) -> List[Dict]:
        """从 trade_logs 已执行记录聚合净持仓（P1-3）"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT stock_code, stock_name, signal_type, shares FROM trade_logs "
                    "WHERE user_action='executed' AND shares > 0 "
                    "AND signal_type IN ('buy','sell','t0_buy','t0_sell') "
                    "ORDER BY date, time"
                )
                rows = cursor.fetchall()
        except Exception as e:
            logger.error("读取已执行交易失败: %s", e)
            return []
        net: Dict[str, Dict] = {}
        for r in rows:
            code = r["stock_code"]
            d = net.setdefault(code, {"code": code, "stock_name": r["stock_name"], "shares": 0})
            sign = 1 if r["signal_type"] in ("buy", "t0_buy") else -1
            d["shares"] += sign * int(r["shares"] or 0)
        for code in net:
            net[code]["cost_price"] = self._last_executed_buy_price(code)
        return [v for v in net.values() if v["shares"] > 0]

    def _last_executed_buy_price(self, code: str) -> float:
        """每只取最近一次 executed 买入的 actual_price（未回填用 trigger_price）"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT actual_price, trigger_price FROM trade_logs "
                    "WHERE stock_code=? AND user_action='executed' "
                    "AND signal_type IN ('buy','t0_buy') "
                    "ORDER BY date DESC, time DESC, id DESC LIMIT 1",
                    (code,),
                )
                row = cursor.fetchone()
                if row:
                    return float((row["actual_price"] or 0) or (row["trigger_price"] or 0))
        except Exception as e:
            logger.error("读取买入均价失败 %s: %s", code, e)
        return 0.0

    # ============================================================
    # 【六】记录闭环：开仓持仓（含假说） / 已平仓配对 / 归因回执
    # ============================================================

    def get_open_position(self, code: str) -> Optional[Dict]:
        """读取该股当前开仓行（最后一次 executed 买入，且其后无 executed 卖出平掉）。

        返回含：log_id, entry_type, paired_z, paired_w_low, paired_w_high,
        z_reference, entry_price(actual), trigger_price, hypothesis_sentence,
        date, shares —— 供 check_exit_signals 的策略配对出场（Z/W）消费。
        无开仓返回 None。
        """
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT * FROM trade_logs
                       WHERE stock_code=? AND user_action='executed'
                       AND signal_type IN ('buy','t0_buy')
                       ORDER BY date DESC, time DESC, id DESC""",
                    (code,),
                )
                buys = [dict(r) for r in cursor.fetchall()]
                if not buys:
                    return None
                cursor.execute(
                    """SELECT * FROM trade_logs
                       WHERE stock_code=? AND user_action='executed'
                       AND signal_type IN ('sell','t0_sell')
                       ORDER BY date DESC, time DESC, id DESC""",
                    (code,),
                )
                sells = [dict(r) for r in cursor.fetchall()]

                def _key(r):
                    return (str(r.get("date") or ""), str(r.get("time") or ""), int(r.get("id") or 0))

                last_buy = buys[0]
                last_sell = sells[0] if sells else None
                # 有更晚的 executed 卖出 → 视为已平仓（信号服务模式下的粗粒度配对）
                if last_sell and _key(last_sell) > _key(last_buy):
                    return None
                # 已回填 exit_price 的开仓行也已平仓
                if last_buy.get("exit_price"):
                    return None
                position = dict(last_buy)
                position["log_id"] = last_buy.get("id")
                position["entry_price"] = float(
                    (last_buy.get("actual_price") or 0) or (last_buy.get("trigger_price") or 0)
                )
                return position
        except Exception as e:
            logger.error("读取开仓持仓失败 %s: %s", code, e)
            return None

    def get_closed_trades(self) -> List[Dict]:
        """【六】已平仓交易列表（回执联动的配对结果，供分层统计/下线判定）。

        每条: {stock_code, strategy(entry_type), pnl_pct, zw_triggered,
              review_outcome, entry_date, exit_date}
        """
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT stock_code, stock_name, entry_type, date, exit_date,
                              pnl_pct, zw_triggered, review_outcome
                       FROM trade_logs
                       WHERE user_action='executed'
                       AND signal_type IN ('buy','t0_buy')
                       AND exit_price IS NOT NULL AND exit_price > 0
                       AND pnl_pct IS NOT NULL
                       ORDER BY date, time, id"""
                )
                rows = [dict(r) for r in cursor.fetchall()]
            for r in rows:
                r["strategy"] = r.pop("entry_type") or "未知策略"
                r["entry_date"] = r.pop("date")
            return rows
        except Exception as e:
            logger.error("读取已平仓交易失败: %s", e)
            return []

    def set_review_outcome(self, log_id: int, outcome: str, note: str = "") -> bool:
        """【六】事后归因回执：logic_right（逻辑对了）/ luck（运气）/ logic_wrong（逻辑错了）"""
        if outcome not in ("logic_right", "luck", "logic_wrong"):
            return False
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE trade_logs SET review_outcome = ?, review_note = ? WHERE id = ?",
                    (outcome, note, log_id),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("归因回执失败 #%s: %s", log_id, e)
            return False

    def get_pending_signals(self, target_date: Optional[str] = None) -> List[Dict]:
        """获取待回执信号（user_action='pending'），供回执脚本列出（P1-3）"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                if target_date:
                    cursor.execute(
                        "SELECT * FROM trade_logs WHERE user_action='pending' AND date=? ORDER BY time",
                        (target_date,),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM trade_logs WHERE user_action='pending' ORDER BY date DESC, time")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("获取待回执信号失败: %s", e)
            return []

    def get_account_summary(self) -> Dict:
        """获取账户摘要（P0-1）。当前无现金流水表，返回默认总资产 1,000,000。

        后续接入真实账户接口后替换该实现。
        """
        return {"total_asset": 1_000_000}

    def get_today_logs(self) -> List[Dict]:
        """获取今日日志"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = date.today().isoformat()
            cursor.execute(
                "SELECT * FROM trade_logs WHERE date = ? ORDER BY time ASC",
                (today,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("获取今日日志失败: %s", e)
            return []
        finally:
            conn.close()

    def get_logs_by_date(self, target_date: str) -> List[Dict]:
        """获取指定日期日志"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trade_logs WHERE date = ? ORDER BY time ASC",
                (target_date,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("获取日志失败: %s", e)
            return []
        finally:
            conn.close()

    def get_signal_stats(self, days: int = 7) -> Dict:
        """获取近N天信号统计"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            start_date = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
            cursor.execute(
                "SELECT signal_type, user_action, COUNT(*) as cnt FROM trade_logs WHERE date >= ? GROUP BY signal_type, user_action",
                (start_date,),
            )
            stats = {}
            for row in cursor.fetchall():
                key = f"{row['signal_type']}_{row['user_action']}"
                stats[key] = row["cnt"]
            return stats
        except Exception as e:
            logger.error("获取信号统计失败: %s", e)
            return {}
        finally:
            conn.close()

    def get_today_t0_rounds(self, stock_code: str) -> int:
        """
        获取今日指定个股的做T轮数

        一轮做T = 一次 t0_buy + 一次 t0_sell 配对。
        为简化判断，这里以 t0_buy 的次数作为轮数（每轮触发一次开仓）。
        若仅有 t0_sell 而无 t0_buy，按 0.5 轮计入。

        Args:
            stock_code: 股票代码

        Returns:
            今日该股做T轮数
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = date.today().isoformat()
            cursor.execute(
                "SELECT signal_type, COUNT(*) as cnt FROM trade_logs "
                "WHERE date = ? AND stock_code = ? AND signal_type IN ('t0_buy', 't0_sell') "
                "GROUP BY signal_type",
                (today, stock_code),
            )
            counts = {row["signal_type"]: row["cnt"] for row in cursor.fetchall()}
            buy_count = counts.get("t0_buy", 0)
            sell_count = counts.get("t0_sell", 0)
            # 每 1 次 buy 视为 1 轮；若仅有 sell 而无 buy，按 0.5 轮计
            return buy_count + (0.5 if buy_count == 0 and sell_count > 0 else 0)
        except Exception as e:
            logger.error("获取今日做T轮数失败: %s", e)
            return 0
        finally:
            conn.close()

    def get_today_unclosed_t0_positions(self) -> List[Dict]:
        """
        获取今日未了结的 T 仓列表

        判定逻辑：今日某只股票有 t0_buy 但 t0_buy 次数 > t0_sell 次数，
        说明仍有未平仓的 T 仓。

        Returns:
            [{"stock_code": ..., "stock_name": ..., "buy_count": ..., "sell_count": ...}, ...]
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = date.today().isoformat()
            cursor.execute(
                "SELECT stock_code, stock_name, signal_type, COUNT(*) as cnt "
                "FROM trade_logs WHERE date = ? AND signal_type IN ('t0_buy', 't0_sell') "
                "GROUP BY stock_code, stock_name, signal_type",
                (today,),
            )
            stock_map: Dict[str, Dict] = {}
            for row in cursor.fetchall():
                code = row["stock_code"]
                if code not in stock_map:
                    stock_map[code] = {
                        "stock_code": code,
                        "stock_name": row["stock_name"],
                        "buy_count": 0,
                        "sell_count": 0,
                    }
                if row["signal_type"] == "t0_buy":
                    stock_map[code]["buy_count"] = row["cnt"]
                else:
                    stock_map[code]["sell_count"] = row["cnt"]
            # 仅返回 buy > sell 的标的
            return [
                v for v in stock_map.values()
                if v["buy_count"] > v["sell_count"]
            ]
        except Exception as e:
            logger.error("获取未了结T仓失败: %s", e)
            return []
        finally:
            conn.close()


# 单例
_instance: Optional[TradeLogger] = None


def get_trade_logger() -> TradeLogger:
    global _instance
    if _instance is None:
        _instance = TradeLogger()
    return _instance
