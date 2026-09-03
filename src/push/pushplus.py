"""
PushPlus 微信推送
免费版每日200条限制，有频率限制（约5 req/s）
"""
import logging
import time
import threading
from typing import Dict, Optional, List
from datetime import datetime

import requests
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


class PushPlus:
    """PushPlus 微信推送"""

    # 频率限制：两次推送之间至少间隔 1.2 秒
    MIN_INTERVAL = 1.2

    # PushPlus 单条内容上限 2 万字，留 10% 余量避免边界误判
    MAX_CONTENT_LEN = 18000
    # 超长内容分块时优先在安全边界断开，避免切断 HTML 标签触发服务端 999 校验
    _CHUNK_BOUNDARIES = ("<br/>", "<br>", "<hr/>", "<hr>", "</table>", "</div>", "\n")

    def __init__(self, config_path: Optional[str] = None):
        config_file = config_path or str(CONFIG_DIR / "push.yaml")
        with open(config_file, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

        self._token = self._config.get("pushplus", {}).get("token", "")
        self._daily_limit = self._config.get("pushplus", {}).get("daily_limit", 200)
        self._api_url = "http://www.pushplus.plus/send"
        self._sent_count = 0
        self._lock = threading.Lock()  # P1-11: 并发推送保护
        self._sent_today = datetime.now().strftime("%Y-%m-%d")
        self._last_send_time = 0.0  # 上次发送时间戳

    def send(
        self,
        title: str,
        content: str,
        template: str = "html",
        level: str = "常规",
    ) -> bool:
        """
        发送消息（超长内容自动分批推送）

        Args:
            title: 消息标题（分批时自动追加 (i/N) 后缀）
            content: 消息内容（支持HTML；超过 PushPlus 单条上限 2 万字时自动分块）
            template: 模板类型 html/txt
            level: 消息级别 常规/重要/紧急

        Returns:
            是否全部批次发送成功
        """
        # 重置日计数
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._sent_today:
            self._sent_count = 0
            self._sent_today = today

        # 检查每日限额
        if self._sent_count >= self._daily_limit:
            logger.warning("PushPlus daily limit reached: %d/%d", self._sent_count, self._daily_limit)
            return False

        if not self._token or self._token == "your-pushplus-token":
            logger.warning("PushPlus token not configured, skipping push: %s", title)
            return False

        # P-修复（2026-08-31）：单条内容超过服务端 2 万字上限会被拒（999 发送内容过大），
        # 按安全边界分块、分批推送；每批标题追加 (i/N) 后缀便于用户串起来读
        chunks = self._chunk_content(content)
        if len(chunks) > 1:
            logger.warning("PushPlus 内容过长(%d字>%d)，拆分为 %d 条推送",
                           len(content), self.MAX_CONTENT_LEN, len(chunks))

        all_ok = True
        for i, chunk in enumerate(chunks):
            chunk_title = title if len(chunks) == 1 else f"{title} ({i + 1}/{len(chunks)})"
            if not self._send_one(chunk_title, chunk, template, level):
                all_ok = False
        return all_ok

    @staticmethod
    def _chunk_content(content: str, max_len: int = None) -> List[str]:
        """按安全边界将超长内容分块，避免切断 HTML 标签触发服务端校验。

        优先在 <br/> <hr/> </table> </div> 换行等边界断开；
        单块内找不到安全边界时硬截断兜底（避免死循环）。
        """
        max_len = max_len or PushPlus.MAX_CONTENT_LEN
        if len(content) <= max_len:
            return [content]
        chunks: List[str] = []
        start = 0
        while start < len(content):
            if len(content) - start <= max_len:
                chunks.append(content[start:])
                break
            window = content[start:start + max_len]
            cut = -1
            for b in PushPlus._CHUNK_BOUNDARIES:
                idx = window.rfind(b)
                if idx >= 0 and idx + len(b) > cut:
                    cut = idx + len(b)
            if cut <= 0:
                cut = max_len  # 兜底：窗口内无安全边界
            chunks.append(content[start:start + cut])
            start += cut
        return chunks

    def _send_one(self, title: str, content: str, template: str, level: str) -> bool:
        """单条 HTTP 发送（含频率限制、每日限额、html→txt 降级重试）。"""
        # 分批场景下逐条校验每日限额，避免批次把配额打穿
        if self._sent_count >= self._daily_limit:
            logger.warning("PushPlus daily limit reached: %d/%d", self._sent_count, self._daily_limit)
            return False

        # 频率限制：避免触发 PushPlus 服务端限流
        now = time.time()
        elapsed = now - self._last_send_time
        if elapsed < self.MIN_INTERVAL:
            wait = self.MIN_INTERVAL - elapsed
            logger.debug("PushPlus rate limit: waiting %.2fs", wait)
            time.sleep(wait)

        payload = {
            "token": self._token,
            "title": title,
            "content": content,
            "template": template,
        }

        try:
            response = requests.post(self._api_url, json=payload, timeout=10)
            result = response.json()

            if result.get("code") == 200:
                self._sent_count += 1
                self._last_send_time = time.time()
                logger.info("Push sent: [%s] %s (count: %d/%d)", level, title, self._sent_count, self._daily_limit)
                return True
            else:
                # 完整落日志（含 data 字段），供定位 999 服务端验证错误的具体原因
                logger.error("Push failed: code=%s msg=%s data=%s",
                             result.get("code"), result.get("msg"), result.get("data"))
                # P-修复（2026-08-31）：html 模板被服务端拒绝（畸形 HTML/内容校验触发 999/600/500）时，
                # 降级为 txt 模板重试一次，保证信号详情仍能送达；仍失败则按失败处理
                if template == "html" and str(result.get("code")) in ("999", "600", "500"):
                    time.sleep(self.MIN_INTERVAL)
                    payload["template"] = "txt"
                    logger.warning("html 模板推送被拒(%s)，降级 txt 重试: %s", result.get("msg"), title)
                    response = requests.post(self._api_url, json=payload, timeout=10)
                    result2 = response.json()
                    if result2.get("code") == 200:
                        self._sent_count += 1
                        self._last_send_time = time.time()
                        logger.info("Push sent(txt fallback): [%s] %s (count: %d/%d)", level, title, self._sent_count, self._daily_limit)
                        return True
                    logger.error("Push txt fallback failed: code=%s msg=%s data=%s",
                                 result2.get("code"), result2.get("msg"), result2.get("data"))
                return False

        except Exception as e:
            logger.error("Push error: %s", e)
            return False

    def send_entry_signal(self, signal_data: Dict) -> bool:
        """发送买入信号"""
        from .templates import render_entry_signal
        title, content = render_entry_signal(signal_data)
        return self.send(title, content, level="重要")

    def send_entry_signals_batch(self, signals_list: List[Dict]) -> bool:
        """批量发送买入信号（合并为一条推送，避免频率限制）"""
        if not signals_list:
            return True
        from .templates import render_entry_signals_batch
        title, content = render_entry_signals_batch(signals_list)
        return self.send(title, content, level="重要")

    def send_exit_signal(self, signal_data: Dict) -> bool:
        """发送卖出信号"""
        from .templates import render_exit_signal
        title, content = render_exit_signal(signal_data)
        level = "紧急" if signal_data.get("exit_type") == "破位止损" else "重要"
        return self.send(title, content, level=level)

    def send_exit_signals_batch(self, signals_list: List[Dict]) -> bool:
        """批量发送卖出信号（合并为一条推送）"""
        if not signals_list:
            return True
        from .templates import render_exit_signals_batch
        title, content = render_exit_signals_batch(signals_list)
        has_urgent = any(
            s.get("exit_type") == "破位止损" or s.get("urgency") == "紧急"
            for s in signals_list
        )
        level = "紧急" if has_urgent else "重要"
        return self.send(title, content, level=level)

    def send_t0_signal(self, signal_data: Dict) -> bool:
        """发送做T信号"""
        from .templates import render_t0_signal
        title, content = render_t0_signal(signal_data)
        return self.send(title, content, level="重要")

    def send_insight_signal(self, signal_data: Dict) -> bool:
        """发送观点兑现/证伪信号"""
        from .templates import render_insight_signal
        title, content = render_insight_signal(signal_data)
        return self.send(title, content, level="重要")

    def send_pre_market(self, summary: str) -> bool:
        """发送盘前计划（保留兼容，实际已合并到 send_intraday_report）"""
        return self.send("📊 盘前计划", summary, level="常规")

    def send_intraday_report(self, environment: Dict, entries: List[Dict] = None,
                             exits: List[Dict] = None, observations: List[Dict] = None) -> bool:
        """
        发送盘中统一报告：环境总览 + 买卖信号 + 观察（合并为一条推送）。

        展示逻辑：
          - 买入信号 → "买入" 板块
          - 卖出信号 → "卖出" 板块（不再按 urgency 拆分，卖出就是卖出）
          - 无买卖信号的持仓股 → "观察" 板块

        Args:
            environment: 环境评估数据
            entries: 买入信号列表
            exits: 卖出信号列表（全部作为卖出，不叫观察）
            observations: 无买卖信号的持仓股列表（这才是观察）

        Returns:
            是否发送成功
        """
        entries = entries or []
        exits = exits or []
        observations = observations or []

        from .templates import render_environment_overview, render_entry_signal, render_exit_signal

        # 标题
        mode = environment.get("market_mode", "defend")
        score = environment.get("market_score", 5.0)
        mn = {"attack": "进攻", "defend": "防守", "retreat": "撤退"}.get(mode, mode)

        title_parts = [f"{mn} {score:.1f}分"]
        if entries:
            title_parts.append(f"买{len(entries)}")
        if exits:
            title_parts.append(f"卖{len(exits)}")
        if observations:
            title_parts.append(f"观察{len(observations)}")
        title = " | ".join(title_parts)

        # 内容：环境总览 + 信号
        content = render_environment_overview(environment)

        if entries:
            content += f"<b>📥 买入信号 ({len(entries)}条)</b><br/><br/>"
            for i, s in enumerate(entries):
                _, card = render_entry_signal(s)
                content += card
                if i < len(entries) - 1:
                    content += "<br/><hr/>"
            content += "<br/>"

        if exits:
            content += f"<b>📤 卖出信号 ({len(exits)}条)</b><br/><br/>"
            for i, s in enumerate(exits):
                _, card = render_exit_signal(s)
                content += card
                if i < len(exits) - 1:
                    content += "<br/>"
            content += "<br/>"

        if observations:
            content += f"<b>📋 观察 ({len(observations)}条)</b><br/><br/>"
            for i, s in enumerate(observations):
                _, card = render_exit_signal(s)
                content += card
                if i < len(observations) - 1:
                    content += "<br/><hr/>"
            content += "<br/>"

        # 级别
        has_urgent = any(s.get("urgency") == "紧急" for s in exits)
        level = "紧急" if has_urgent else ("重要" if (entries or exits) else "常规")

        return self.send(title, content, level=level)

    def send_daily_review(self, review: str) -> bool:
        """发送盘后复盘"""
        return self.send("📈 盘后复盘", review, level="常规")

    def send_weekly_report(self, report: str) -> bool:
        """发送周报"""
        return self.send("📊 周度报告", report, level="常规")

    def send_force_close_remind(self, stock_name=None, stock_code=None, direction=None, content=None) -> bool:
        """
        发送T仓强制了结提醒

        支持两种调用方式：
        1. 单标的：send_force_close_remind(stock_name="XX", stock_code="600XXX", direction="正T")
        2. 批量：send_force_close_remind(content="<multi-stock HTML>")

        Args:
            stock_name: 单标的模式下的股票名称
            stock_code: 单标的模式下的股票代码
            direction: 单标的模式下的做T方向
            content: 批量模式下的完整 HTML 内容（覆盖默认模板）

        Returns:
            是否发送成功
        """
        title = "🔴【紧急】T仓未了结提醒"
        if content is not None:
            # 批量模式：直接使用调用方提供的 HTML 内容
            return self.send(title, content, level="紧急")

        # 单标的模式：使用默认模板
        content = f"""
        <b>{stock_name}({stock_code})</b><br/>
        <br/>
        📦 做T方向：{direction}<br/>
        ⚠️ 今日必须了结！距离收盘仅剩10分钟<br/>
        <br/>
        <b>请立即手动了结T仓！</b>
        """
        return self.send(title, content, level="紧急")

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def remaining(self) -> int:
        return self._daily_limit - self._sent_count


# 单例
_instance: Optional[PushPlus] = None


def get_pushplus() -> PushPlus:
    global _instance
    if _instance is None:
        _instance = PushPlus()
    return _instance
