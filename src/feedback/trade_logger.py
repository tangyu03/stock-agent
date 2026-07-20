"""
交易日志
记录所有信号和用户操作
"""
import logging
import json
from typing import Dict, Any, Optional, List
from datetime import datetime, date

from ..db import get_connection

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
    ):
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
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute(
                """INSERT INTO trade_logs
                (date, time, stock_code, stock_name, signal_type, entry_type, exit_type,
                 trigger_price, stop_loss, target_price, suggested_position,
                 mode_at_signal, sector_status, market_score,
                 user_action, actual_price, actual_position, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M:%S"),
                    stock_code,
                    stock_name,
                    signal_type,
                    signal_data.get("entry_type", ""),
                    signal_data.get("exit_type", ""),
                    signal_data.get("trigger_price", 0),
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
        except Exception as e:
            logger.error("记录交易日志失败: %s", e)
        finally:
            conn.close()

    def update_action(self, log_id: int, user_action: str, actual_price: float = 0, actual_position: float = 0):
        """更新用户操作"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE trade_logs SET user_action = ?, actual_price = ?, actual_position = ? WHERE id = ?",
                (user_action, actual_price, actual_position, log_id),
            )
            conn.commit()
        except Exception as e:
            logger.error("更新交易日志失败: %s", e)
        finally:
            conn.close()

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
