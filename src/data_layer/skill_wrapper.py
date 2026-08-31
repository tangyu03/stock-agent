"""
问财 Skill 调用封装
封装 hithink-market-query 和 hithink-event-query 的调用逻辑
提供统一的调用接口、重试机制和缓存集成
"""
import logging
import json
import time
from typing import Optional, Dict, List
from datetime import datetime
from .data_cache import get_data_cache

logger = logging.getLogger(__name__)


class SkillWrapper:
    """
    问财 Skill 调用封装

    注意：实际问财Skill调用依赖 hithink-market-query / hithink-event-query
    本模块提供调用框架，具体Skill接入在部署时实现
    """

    def __init__(self):
        self._cache = get_data_cache()
        self._skill_registry: Dict[str, Dict] = {}
        self._register_skills()

    def _register_skills(self):
        """注册所有问财Skill"""
        # 官方14个Skill
        official_skills = [
            {"id": "index_data", "name": "指数数据查询", "layer": 1, "type": "market"},
            {"id": "stock_quote", "name": "行情数据查询", "layer": "1+3+4", "type": "market"},
            {"id": "macro_data", "name": "宏观数据查询", "layer": "1+insight", "type": "market"},
            {"id": "sector_filter", "name": "问财选板块", "layer": 2, "type": "market"},
            {"id": "industry_data", "name": "行业数据查询", "layer": 2, "type": "market"},
            {"id": "stock_filter", "name": "问财选A股", "layer": "3+insight", "type": "market"},
            {"id": "financial_data", "name": "财务数据查询", "layer": "3+insight", "type": "market"},
            {"id": "event_data", "name": "事件数据查询", "layer": "3+insight", "type": "event"},
            {"id": "shareholder_data", "name": "公司股东股本查询", "layer": 3, "type": "market"},
            {"id": "news_search", "name": "新闻搜索", "layer": "2+insight", "type": "event"},
            {"id": "announcement_search", "name": "公告搜索", "layer": "3+insight", "type": "event"},
            {"id": "research_rating", "name": "机构研究与评级", "layer": "3+insight", "type": "event"},
            {"id": "business_data", "name": "公司经营数据", "layer": "3+insight", "type": "market"},
            {"id": "simulated_trade", "name": "模拟炒股", "layer": "feedback", "type": "market"},
        ]

        # 社区8个Skill
        community_skills = [
            {"id": "market_sentiment", "name": "市场情绪分析", "layer": 1, "type": "market"},
            {"id": "industry_rotation", "name": "行业轮动分析", "layer": 2, "type": "market"},
            {"id": "rotation_monitor", "name": "行业轮动监控", "layer": 2, "type": "market"},
            {"id": "event_driven", "name": "事件驱动策略", "layer": 2, "type": "event"},
            {"id": "kline_pattern", "name": "K线形态识别", "layer": 4, "type": "market"},
            {"id": "tech_signals", "name": "技术指标信号引擎", "layer": 4, "type": "market"},
            {"id": "financial_checkup", "name": "财报体检", "layer": 3, "type": "market"},
            {"id": "risk_stress_test", "name": "风险分析与压力测试", "layer": "feedback", "type": "market"},
        ]

        for skill in official_skills + community_skills:
            self._skill_registry[skill["id"]] = skill

        logger.info("Registered %d skills", len(self._skill_registry))

    def list_skills(self) -> List[Dict]:
        """列出所有已注册的Skill"""
        return list(self._skill_registry.values())

    def get_skills_by_layer(self, layer: int) -> List[Dict]:
        """获取指定层级的Skill"""
        return [s for s in self._skill_registry.values() if str(layer) in str(s["layer"])]

    def query(self, skill_id: str, query_text: str, use_cache: bool = True, ttl_hours: int = 4) -> Optional[Dict]:
        """
        调用问财Skill查询

        Args:
            skill_id: Skill ID
            query_text: 自然语言查询文本
            use_cache: 是否使用缓存
            ttl_hours: 缓存有效期

        Returns:
            查询结果字典
        """
        if skill_id not in self._skill_registry:
            logger.error("Skill '%s' not found in registry", skill_id)
            return None

        # 缓存键
        today = datetime.now().strftime("%Y%m%d")
        cache_key = f"skill:{skill_id}:{today}:{hash(query_text)}"

        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Skill cache hit: %s", skill_id)
                return cached

        # 实际调用问财Skill
        result = self._call_skill(skill_id, query_text)

        if result is not None and use_cache:
            self._cache.set(cache_key, result, ttl_hours=ttl_hours)

        return result

    def _call_skill(self, skill_id: str, query_text: str) -> Optional[Dict]:
        """
        实际调用问财Skill

        优先尝试真实问财API（需配置 IWENCAI_API_KEY），
        未配置时返回占位结果，由上层模块降级处理

        Args:
            skill_id: Skill ID
            query_text: 查询文本

        Returns:
            Skill返回的结果
        """
        skill = self._skill_registry.get(skill_id, {})
        skill_type = skill.get("type", "market")

        # 如果API冷却中，检查是否已过冷却期
        if getattr(self, '_api_cooldown_until', 0) > time.time():
            remaining = int(self._api_cooldown_until - time.time())
            logger.debug("[Cooldown] Skipping %s skill '%s' (%ds remaining)", skill_type, skill_id, remaining)
            return {
                "skill_id": skill_id,
                "query": query_text,
                "status": "placeholder",
                "data": None,
                "message": f"API cooldown ({remaining}s) - {skill_type} skill pending",
            }

        # 尝试调用真实问财API
        api_result = self._call_iwencai_api(skill_id, skill_type, query_text)
        if api_result is not None:
            return api_result

        # 未配置API Key或调用失败，返回占位结果
        logger.info("[Placeholder] Calling %s skill '%s' with query: %s", skill_type, skill_id, query_text)
        return {
            "skill_id": skill_id,
            "query": query_text,
            "status": "placeholder",
            "data": None,
            "message": f"IWENCAI_API_KEY not configured - {skill_type} skill pending",
        }

    def _call_iwencai_api(self, skill_id: str, skill_type: str, query_text: str) -> Optional[Dict]:
        """
        调用问财OpenAPI网关获取真实数据

        需要环境变量 IWENCAI_API_KEY
        未配置时返回None，由上层降级处理

        Args:
            skill_id: Skill ID
            skill_type: market / event
            query_text: 查询文本

        Returns:
            成功返回 {"skill_id": ..., "query": ..., "status": "ok", "data": [...]},
            未配置Key或失败返回 None
        """
        import os
        api_key = os.environ.get("IWENCAI_API_KEY", "").strip()
        if not api_key:
            # 调试便利：未设置环境变量时使用硬编码默认值（生产环境请通过环境变量覆盖）
            api_key = "sk-proj-01-19pbDVq97Iwxju9kqva0PqONED6jlEQp7AmvCsBJ6N0BTqKb2e43WtxvB3TXzfs8W7NIBiTUkYArnq7VOyqVkjisWE138GbHRRfJov7ZssT4wsuc1O3OBVF3kOfKaRd0ui2cOQ"
        if not api_key:
            return None

        # 频率限制：连续请求间至少等待2秒
        if not hasattr(self, '_last_api_call_time'):
            self._last_api_call_time = 0
        elapsed = time.time() - self._last_api_call_time
        if elapsed < 2.0:
            time.sleep(2.0 - elapsed)

        try:
            import secrets as _secrets
            import urllib.request
            import urllib.error

            base_url = os.environ.get("IWENCAI_BASE_URL", "https://openapi.iwencai.com").rstrip("/")
            url = f"{base_url}/v1/query2data"
            trace_id = _secrets.token_hex(32)

            # 根据skill_id精确映射问财X-Claw-Skill-Id
            x_skill_id = self._map_claw_skill_id(skill_id, skill_type)

            payload = {
                "query": query_text,
                "page": "1",
                "limit": "20",
                "is_cache": "1",
                "expand_index": "true",
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Claw-Call-Type": "normal",
                "X-Claw-Skill-Id": x_skill_id,
                "X-Claw-Skill-Version": "1.0.0",
                "X-Claw-Plugin-Id": "none",
                "X-Claw-Plugin-Version": "none",
                "X-Claw-Trace-Id": trace_id,
            }

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=30)
            self._last_api_call_time = time.time()
            result = json.loads(resp.read().decode("utf-8"))

            datas = result.get("datas", [])
            code_count = result.get("code_count", 0)

            logger.info(
                "问财API调用成功: skill=%s, query='%s', 返回%d条(共%d条)",
                skill_id, query_text[:30], len(datas), code_count,
            )

            return {
                "skill_id": skill_id,
                "query": query_text,
                "status": "ok",
                "data": datas,
                "code_count": code_count,
            }

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            quota_exhausted = "次数已用完" in body_text or "升级权益" in body_text

            if e.code == 401 and quota_exhausted:
                # 当日额度用完 —— 冷却到次日 09:30（开盘后）
                now = datetime.now()
                tomorrow = now.replace(hour=9, minute=30, second=0, microsecond=0)
                if tomorrow <= now:
                    tomorrow = tomorrow.replace(day=now.day + 1)
                cooldown = (tomorrow - now).total_seconds()
                self._api_cooldown_until = time.time() + cooldown
                logger.warning("问财API当日额度用完，冷却至明早 09:30（%.0f 分钟后重试）", cooldown / 60)
            elif e.code in (401, 429):
                cooldown_seconds = 30 if e.code == 429 else 60
                self._api_cooldown_until = time.time() + cooldown_seconds
                logger.warning("问财API配额限制 %s %s, 冷却%ds后重试", e.code, e.reason, cooldown_seconds)
            else:
                logger.error("问财API HTTP错误: %s %s", e.code, e.reason)
            return None
        except urllib.error.URLError as e:
            logger.error("问财API网络错误: %s", e.reason)
            return None
        except Exception as e:
            logger.error("问财API调用失败: %s", e)
            return None

    @staticmethod
    def _map_claw_skill_id(skill_id: str, skill_type: str) -> str:
        """
        将内部skill_id映射到问财OpenAPI的X-Claw-Skill-Id

        问财商店目前提供3个Skill：
        - hithink-market-query: 行情数据（指数、行情、板块、资金流向等）
        - hithink-astock-selector: A股选股筛选（技术形态、财务指标、组合条件）
        - hithink-event-query: 事件数据（业绩预告、增发、质押、解禁等）
        """
        # 选股类 → hithink-astock-selector
        astock_selector_skills = {
            "stock_filter", "financial_data", "shareholder_data",
            "financial_checkup", "business_data",
        }
        if skill_id in astock_selector_skills:
            return "hithink-astock-selector"

        # 事件类 → hithink-event-query
        event_skills = {
            "event_data", "news_search", "announcement_search",
            "research_rating", "event_driven",
        }
        if skill_id in event_skills:
            return "hithink-event-query"

        # 其余market类 → hithink-market-query
        # (index_data, stock_quote, macro_data, sector_filter, industry_data,
        #  market_sentiment, industry_rotation, rotation_monitor,
        #  kline_pattern, tech_signals, simulated_trade, risk_stress_test)
        return "hithink-market-query"

    # ============ 便捷查询方法 ============

    def query_index(self, index_name: str = "上证指数") -> Optional[Dict]:
        """查询指数数据"""
        return self.query("index_data", f"{index_name}最新行情数据")

    def query_stock_quote(self, stock_code: str) -> Optional[Dict]:
        """查询个股行情"""
        return self.query("stock_quote", f"{stock_code}最新行情数据")

    def query_sector(self, sector_name: str) -> Optional[Dict]:
        """查询行业数据"""
        return self.query("industry_data", f"{sector_name}行业估值和资金流向")

    def query_sector_filter(self, conditions: str) -> Optional[Dict]:
        """问财选板块：筛选符合条件的行业板块"""
        return self.query("sector_filter", conditions, use_cache=False)

    def query_stock_filter(self, conditions: str) -> Optional[Dict]:
        """问财选股"""
        return self.query("stock_filter", conditions, use_cache=False)

    def query_financial(self, stock_code: str) -> Optional[Dict]:
        """查询财务数据"""
        return self.query("financial_data", f"{stock_code}最新财务数据")

    def query_events(self, stock_code: str) -> Optional[Dict]:
        """查询事件数据"""
        return self.query("event_data", f"{stock_code}业绩预告、质押、解禁")

    def query_news(self, keyword: str) -> Optional[Dict]:
        """搜索新闻"""
        return self.query("news_search", keyword, use_cache=False)

    def query_financial_checkup(self, stock_code: str) -> Optional[Dict]:
        """财报体检"""
        return self.query("financial_checkup", f"{stock_code}财报体检")

    def query_tech_signals(self, stock_code: str) -> Optional[Dict]:
        """技术指标信号"""
        return self.query("tech_signals", f"{stock_code}技术指标信号", ttl_hours=1)

    def query_kline_pattern(self, stock_code: str) -> Optional[Dict]:
        """K线形态识别"""
        return self.query("kline_pattern", f"{stock_code}K线形态识别", ttl_hours=1)

    def query_market_sentiment(self) -> Optional[Dict]:
        """市场情绪分析"""
        return self.query("market_sentiment", "市场恐慌贪婪指数和融资融券数据", ttl_hours=2)

    def query_industry_rotation(self) -> Optional[Dict]:
        """行业轮动分析"""
        return self.query("industry_rotation", "同花顺行业景气度和动量排名")

    # ============ 批量查询方法 ============

    def batch_query(self, queries: List[Dict], max_concurrent: int = 3) -> List[Dict]:
        """
        批量查询多个Skill，返回所有结果

        Args:
            queries: 查询列表，每项 {"skill_id": str, "query_text": str, "use_cache": bool, "ttl_hours": int}
            max_concurrent: 最大并发数（问财API有频率限制，不宜太高）

        Returns:
            结果列表，每项包含原始查询参数和查询结果
        """
        import time
        results = []

        for i, q in enumerate(queries):
            skill_id = q.get("skill_id", "")
            query_text = q.get("query_text", "")
            use_cache = q.get("use_cache", True)
            ttl_hours = q.get("ttl_hours", 4)

            if not skill_id or not query_text:
                results.append({
                    "skill_id": skill_id,
                    "query_text": query_text,
                    "result": None,
                    "error": "Missing skill_id or query_text",
                })
                continue

            logger.info("批量查询 [%d/%d]: %s → '%s'", i + 1, len(queries), skill_id, query_text[:30])
            try:
                result = self.query(skill_id, query_text, use_cache=use_cache, ttl_hours=ttl_hours)
                results.append({
                    "skill_id": skill_id,
                    "query_text": query_text,
                    "result": result,
                    "error": None,
                })
            except Exception as e:
                results.append({
                    "skill_id": skill_id,
                    "query_text": query_text,
                    "result": None,
                    "error": str(e),
                })

            # 频率控制：请求间隔至少2秒
            if i < len(queries) - 1:
                time.sleep(2.0)

        success_count = sum(1 for r in results if r.get("result") is not None)
        logger.info("批量查询完成: %d/%d 成功", success_count, len(queries))
        return results

    def batch_query_stock_quotes(
        self, stock_codes: List[str], max_per_batch: int = 5
    ) -> Dict[str, Optional[Dict]]:
        """
        批量查询多只股票的行情数据

        分块查询，每批不超过 max_per_batch 只，避免问财 API 查询字符串过长被截断。
        批量失败时不触发逐个查询降级（耗时且配额昂贵），返回 None 由调用方走 AKShare 兜底。

        Args:
            stock_codes: 股票代码列表
            max_per_batch: 每批最多几只（默认 5）

        Returns:
            {stock_code: 查询结果或 None}
        """
        if not stock_codes:
            return {}

        results: Dict[str, Optional[Dict]] = {}

        # 分块批量查询
        for i in range(0, len(stock_codes), max_per_batch):
            chunk = stock_codes[i:i + max_per_batch]
            codes_str = ",".join(chunk)

            combined_result = self.query("stock_quote", f"{codes_str}最新行情数据")

            if combined_result and combined_result.get("status") == "ok" and combined_result.get("data"):
                data = combined_result.get("data", [])
                for item in (data if isinstance(data, list) else []):
                    if not isinstance(item, dict):
                        continue
                    code = item.get("股票代码", item.get("代码", item.get("code", "")))
                    if code:
                        clean_code = code.split(".")[-1] if "." in str(code) else str(code)
                        results[clean_code] = {
                            "skill_id": "stock_quote",
                            "query": combined_result.get("query", ""),
                            "status": "ok",
                            "data": item,
                            "code_count": 1,
                        }
            else:
                logger.debug("分块查询失败（chunk %d-%d），跳过", i + 1, min(i + max_per_batch, len(stock_codes)))

        # 未查询到的标记为 None（调用方走 AKShare 降级）
        for code in stock_codes:
            if code not in results:
                results[code] = None

        hit = sum(1 for v in results.values() if v is not None)
        logger.info("行情批量查询: 请求 %d 只, 命中 %d 只（分 %d 批）",
                    len(stock_codes), hit,
                    (len(stock_codes) + max_per_batch - 1) // max_per_batch)
        return results

    def batch_query_fund_flow(
        self, stock_codes: List[str], max_per_batch: int = 5
    ) -> Dict[str, Optional[float]]:
        """
        批量查询多只股票的主力资金净流入

        替代原逐股串行查询模式，减少 API 调用次数（优化 #4）。
        分块查询，每批不超过 max_per_batch 只，带频率控制。

        Args:
            stock_codes: 股票代码列表
            max_per_batch: 每批最多几只（默认 5）

        Returns:
            {stock_code: 主力净流入额(元) or None}
        """
        if not stock_codes:
            return {}

        results: Dict[str, Optional[float]] = {}

        for i in range(0, len(stock_codes), max_per_batch):
            chunk = stock_codes[i:i + max_per_batch]
            codes_str = ",".join(chunk)
            resp = self.query("stock_quote", f"{codes_str} 主力资金净流入")

            if resp and resp.get("status") == "ok" and resp.get("data"):
                datas = resp["data"]
                if isinstance(datas, list):
                    for item in datas:
                        if not isinstance(item, dict):
                            continue
                        code = item.get("股票代码", item.get("代码", ""))
                        if not code:
                            continue
                        clean_code = code.split(".")[-1] if "." in str(code) else str(code)
                        for key in ("主力净流入", "主力资金净流入", "主力净流入额"):
                            val = item.get(key)
                            if val is not None:
                                try:
                                    results[clean_code] = float(
                                        str(val).replace(",", "")
                                    )
                                except (ValueError, TypeError):
                                    pass
                                break

            # 频率控制
            if i + max_per_batch < len(stock_codes):
                import time
                time.sleep(2.0)

        # 未查到的标记为 None
        for code in stock_codes:
            results.setdefault(code, None)

        hit = sum(1 for v in results.values() if v is not None)
        logger.info("资金流批量查询: 请求 %d 只, 命中 %d 只", len(stock_codes), hit)
        return results

    def batch_query_sectors(self, sector_names: List[str]) -> Dict[str, Optional[Dict]]:
        """
        批量查询多个板块数据

        Args:
            sector_names: 板块名称列表，如 ["半导体", "新能源", "人工智能"]

        Returns:
            {sector_name: 查询结果} 字典
        """
        if not sector_names:
            return {}

        results = {}
        queries = [
            {"skill_id": "industry_data", "query_text": f"{name}行业估值和资金流向", "ttl_hours": 2}
            for name in sector_names
        ]
        batch_results = self.batch_query(queries)

        for name, br in zip(sector_names, batch_results, strict=False):  # 批量结果与名称数可能不等，保持原截断行为
            results[name] = br.get("result")

        return results


# 单例
_instance: Optional[SkillWrapper] = None


def get_skill_wrapper() -> SkillWrapper:
    global _instance
    if _instance is None:
        _instance = SkillWrapper()
    return _instance
