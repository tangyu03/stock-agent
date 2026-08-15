"""
交易日志
记录所有信号和用户操作
"""
import logging
import json
from typing import Dict, Any, Optional, List
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
                cursor.execute(
                    """INSERT INTO trade_logs
                    (date, time, stock_code, stock_name, signal_type, entry_type, exit_type,
                     trigger_price, shares, stop_loss, target_price, suggested_position,
                     mode_at_signal, sector_status, market_score,
                     user_action, actual_price, actual_position, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error("记录交易日志失败: %s", e)
            return None

    def update_action(self, log_id: int, user_action: str, actual_price: float = 0, actual_position: float = 0) -> bool:
        """更新用户操作（回执脚本调用）"""
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE trade_logs SET user_action = ?, actual_price = ?, actual_position = ? WHERE id = ?",
                    (user_action, actual_price, actual_position, log_id),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("更新交易日志失败: %s", e)
            return False

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
