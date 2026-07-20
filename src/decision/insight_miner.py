"""
观点挖掘引擎
提取判断 → 挖掘标的 → 追踪池 → 兑现推送

关键原则：
- 追踪池是"观察区"，自选是"操作区"
- 追踪池标的不产生买卖信号
- 兑现后建议加入自选C类才走择时
- 判断链联动：上游兑现→推送下游预期强化
"""
import logging
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta

from ..config_models import load_config
from ..db import get_connection
from ..llm_client import get_llm_client
from ..data_layer.skill_wrapper import get_skill_wrapper
from ..data_layer.data_cache import get_data_cache
from ..push.pushplus import get_pushplus
from ..push.templates import render_insight_signal

logger = logging.getLogger(__name__)


@dataclass
class Judgment:
    """判断"""
    id: str
    insight_id: str
    judgment: str
    direction: str         # 看多/看空/事件驱动
    time_horizon: str      # 短期/中期/长期
    confidence: str        # 高/中/低
    valid_days: int
    expire_at: str
    status: str = "tracking"  # tracking / confirming / refuted / expired
    refute_count: int = 0
    tags: List[str] = field(default_factory=list)
    targets: List[Dict] = field(default_factory=list)
    verify_config: Dict = field(default_factory=dict)
    track_metrics: List[str] = field(default_factory=list)


@dataclass
class InsightTrackResult:
    """观点追踪结果"""
    judgment_id: str
    judgment_text: str
    status: str
    changes: Dict[str, Any] = field(default_factory=dict)
    should_push: bool = False
    push_type: str = ""      # confirming / refuted / expired
    suggested_targets: List[str] = field(default_factory=list)


class InsightMiner:
    """观点挖掘引擎"""

    def __init__(self):
        self._insights_config = load_config("insights.yaml")
        self._risk_config = load_config("risk.yaml").get("risk", {})
        self._llm = get_llm_client()
        self._skill = get_skill_wrapper()
        self._cache = get_data_cache()
        self._pushplus = get_pushplus()
        self._refute_threshold = self._risk_config.get("verify_defaults", {}).get("refute_count_threshold", 3)

    def process_article(self, article_text: str, source: str = "") -> Optional[Dict]:
        """
        处理用户输入的文章/观点

        Step 1: LLM提取判断
        Step 2: 向下挖掘→关联A股标的
        Step 3: 问财Skills补全标的画像
        Step 4: 写入追踪池

        Args:
            article_text: 文章原文
            source: 来源标注

        Returns:
            处理结果（包含提取的判断和标的）
        """
        logger.info("=== 观点挖掘开始 ===")
        logger.info("来源: %s, 文本长度: %d字", source, len(article_text))

        # Step 1: LLM提取判断
        extraction = self._llm.extract_judgments(article_text)
        if not extraction:
            logger.warning("LLM未提取到有效判断")
            return None

        judgments_data = extraction.get("judgments", [])
        if not judgments_data:
            logger.warning("未提取到可追踪的判断")
            return None

        logger.info("提取到 %d 条判断", len(judgments_data))

        # 生成洞察ID
        today = datetime.now().strftime("%Y%m%d")
        insight_id = f"INS-{today}-{datetime.now().strftime('%H%M%S')}"

        # Step 2+3: 逐判断处理
        processed_judgments = []
        for j_data in judgments_data:
            # 如果LLM没有提供标的，尝试挖掘
            targets = j_data.get("targets", [])
            if not targets:
                targets = self._mine_targets(j_data.get("judgment", ""))

            # 补全标的画像
            for target in targets:
                self._enrich_target_profile(target)

            # 构建验证配置
            verify_config = j_data.get("verify_config", {})
            if not verify_config:
                verify_config = self._generate_verify_config(j_data, targets)

            j_data["targets"] = targets
            j_data["verify_config"] = verify_config
            processed_judgments.append(j_data)

        # Step 4: 写入数据库
        self._save_insight(insight_id, source, article_text, processed_judgments, extraction.get("chains", []))

        # 保存判断链
        for chain in extraction.get("chains", []):
            self._save_chain(chain)

        result = {
            "insight_id": insight_id,
            "source": source,
            "judgments": processed_judgments,
            "chains": extraction.get("chains", []),
        }

        logger.info("观点挖掘完成: %s, %d条判断, %d条关联链", insight_id, len(processed_judgments), len(extraction.get("chains", [])))
        return result

    def daily_track(self) -> List[InsightTrackResult]:
        """
        每日观点追踪
        检查每条判断的兑现/证伪/过期状态

        Returns:
            追踪结果列表
        """
        logger.info("=== 观点每日追踪开始 ===")
        results = []

        conn = get_connection()
        try:
            cursor = conn.cursor()

            # 获取所有活跃判断
            cursor.execute("""
                SELECT j.*, i.source, i.raw_text
                FROM judgments j
                JOIN insights i ON j.insight_id = i.id
                WHERE j.status IN ('tracking', 'confirming')
                ORDER BY j.expire_at ASC
            """)
            rows = cursor.fetchall()

            for row in rows:
                judgment = self._row_to_judgment(row)
                track_result = self._track_judgment(judgment)
                results.append(track_result)

                # 推送
                if track_result.should_push:
                    self._push_track_result(track_result, row["source"])

        except Exception as e:
            logger.error("观点追踪失败: %s", e)
        finally:
            conn.close()

        confirmed = sum(1 for r in results if r.push_type == "confirming")
        refuted = sum(1 for r in results if r.push_type == "refuted")
        expired = sum(1 for r in results if r.push_type == "expired")
        logger.info("观点追踪完成: 兑现%d / 证伪%d / 过期%d", confirmed, refuted, expired)

        return results

    def _track_judgment(self, judgment: Judgment) -> InsightTrackResult:
        """
        追踪单条判断

        Args:
            judgment: 判断对象

        Returns:
            InsightTrackResult
        """
        result = InsightTrackResult(
            judgment_id=judgment.id,
            judgment_text=judgment.judgment,
            status=judgment.status,
        )

        today = date.today()

        # 检查是否过期
        try:
            expire_date = date.fromisoformat(judgment.expire_at) if judgment.expire_at else None
            if expire_date and today > expire_date:
                result.status = "expired"
                result.push_type = "expired"
                result.should_push = True
                self._update_judgment_status(judgment.id, "expired")
                return result
        except ValueError:
            pass

        # 检查兑现/证伪
        verify_config = judgment.verify_config
        if not verify_config or not verify_config.get("metrics"):
            return result

        confirm_count = 0
        refute_count = 0
        changes = {}

        for metric_cfg in verify_config["metrics"]:
            metric_type = metric_cfg.get("type", "")
            metric_value = self._fetch_metric_value(metric_type, judgment.targets, metric_cfg)
            changes[metric_type] = metric_value

            if metric_value is not None:
                confirm_threshold = metric_cfg.get("confirm_threshold")
                refute_threshold = metric_cfg.get("refute_threshold")

                if confirm_threshold is not None and self._check_threshold(metric_value, confirm_threshold, "confirm"):
                    confirm_count += 1
                elif refute_threshold is not None and self._check_threshold(metric_value, refute_threshold, "refute"):
                    refute_count += 1

        result.changes = changes

        # 判断兑现/证伪逻辑
        confirm_logic = verify_config.get("confirm_logic", "all")
        refute_logic = verify_config.get("refute_logic", "any")

        total_metrics = len(verify_config["metrics"])

        is_confirmed = False
        if confirm_logic == "all":
            is_confirmed = confirm_count == total_metrics and total_metrics > 0
        else:
            is_confirmed = confirm_count > 0

        is_refuted = False
        if refute_logic == "any":
            is_refuted = refute_count > 0
        else:
            is_refuted = refute_count == total_metrics and total_metrics > 0

        if is_confirmed:
            result.status = "confirming"
            result.push_type = "confirming"
            result.should_push = True
            self._update_judgment_status(judgment.id, "confirming")

            # 建议加入自选C类
            for target in judgment.targets:
                result.suggested_targets.append(f"{target.get('name', '')}({target.get('code', '')})")
                self._suggest_to_watchlist(target.get("code", ""))

            # 判断链联动
            self._handle_chain_linkage(judgment.id)

        elif is_refuted:
            new_refute_count = judgment.refute_count + 1
            if new_refute_count >= self._refute_threshold:
                result.status = "refuted"
                result.push_type = "refuted"
                result.should_push = True
                self._update_judgment_status(judgment.id, "refuted", new_refute_count)
            else:
                self._update_judgment_status(judgment.id, judgment.status, new_refute_count)

        return result

    def _mine_targets(self, judgment_text: str) -> List[Dict]:
        """
        从判断文本中挖掘关联A股标的
        使用LLM辅助挖掘
        """
        try:
            prompt = f"""
基于以下判断，挖掘关联的A股标的。
判断：{judgment_text}

请输出JSON格式：
[
  {{"code": "股票代码", "name": "股票名称", "role": "该标的在判断中的角色"}}
]
"""
            from ..llm_client import LLMMessage
            response = self._llm.chat([
                LLMMessage(role="system", content="你是A股研究分析师，根据判断挖掘关联标的。"),
                LLMMessage(role="user", content=prompt),
            ])
            targets = self._llm.extract_json(response)
            if isinstance(targets, list):
                return targets
        except Exception as e:
            logger.error("挖掘标的失败: %s", e)

        return []

    def _enrich_target_profile(self, target: Dict):
        """
        补全标的画像（需求 §10.6）

        对每只标的查询：
          ├── 行情数据 → 当前价 / 均线排列 / 近期涨跌幅（AKShare K 线，0 配额）
          ├── 财务数据 → 营收增速 / 净利润增速 / ROE / 负债率（问财，独有）
          ├── 资金数据 → 主力净流入（问财，独有）
          ├── 板块归属 → 所属同花顺行业（AKShare/问财）
          └── 排雷     → 是否ST / 停牌 / 负面事件（AKShare 行情 + 问财事件）
        """
        code = target.get("code", "")
        if not code:
            return

        from ..data_layer.stock_data import batch_get_realtime_quotes, calc_tech_indicators, detect_kline_patterns
        from ..data_layer.akshare_adapter import get_akshare_adapter
        from ..data_layer.sw_industry import normalize_sector

        # 1. AKShare K 线：当前价 / 均线 / 技术指标 / K 线形态（0 问财配额）
        akshare = get_akshare_adapter()
        hist_result = akshare.get_stock_hist(code)
        if hist_result.success and hist_result.data:
            kline = hist_result.data
            if kline and len(kline) >= 20:
                try:
                    closes = [float(k.get("收盘", k.get("close", 0))) for k in kline]
                    target["current_price"] = closes[-1]
                    target["ma5"] = round(sum(closes[-5:]) / 5, 2)
                    target["ma10"] = round(sum(closes[-10:]) / 10, 2)
                    target["ma20"] = round(sum(closes[-20:]) / 20, 2)

                    # 均线排列
                    if target["ma5"] > target["ma10"] > target["ma20"]:
                        target["ma_alignment"] = "多头排列"
                    elif target["ma5"] < target["ma10"] < target["ma20"]:
                        target["ma_alignment"] = "空头排列"
                    else:
                        target["ma_alignment"] = "震荡"

                    # 近期涨跌幅
                    if len(closes) >= 21:
                        target["gain_20d"] = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2)
                    if len(closes) >= 6:
                        target["gain_5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)
                except (KeyError, ValueError, IndexError):
                    pass

            # 技术指标 + K 线形态（需求 §12.2 #5 #6）
            if kline and len(kline) >= 30:
                tech = calc_tech_indicators(kline)
                if tech:
                    target["tech_vote"] = tech.get("vote", "neutral")
                    target["tech_score"] = tech.get("vote_score", 0)
                    target["rsi"] = tech.get("rsi")
                    target["adx"] = tech.get("adx")

                patterns = detect_kline_patterns(kline)
                if patterns:
                    target["kline_patterns"] = [p["pattern"] for p in patterns]

        # 2. AKShare 实时行情：ST/停牌标记（0 问财配额）
        quotes = batch_get_realtime_quotes([code])
        if code in quotes:
            q = quotes[code]
            if not target.get("current_price"):
                target["current_price"] = q["current_price"]
            target["stock_name"] = q.get("name", target.get("name", ""))
            target["is_st"] = q.get("is_st", False)
            target["is_suspended"] = q.get("is_suspended", False)

        # 3. 问财：财务数据 → 营收增速 / 净利润增速 / ROE / 负债率（需求 §10.6）
        try:
            financial = self._skill.query_financial(code)
            if financial and financial.get("data"):
                fin_data = financial["data"]
                if isinstance(fin_data, list) and fin_data:
                    fin_data = fin_data[0] if isinstance(fin_data[0], dict) else {}
                if isinstance(fin_data, dict):
                    # 提取关键财务指标（问财返回字段名可能多样）
                    for field_names, target_key in [
                        (["营业收入同比增长", "营收增速", "营收同比", "revenue_growth"], "revenue_growth"),
                        (["净利润同比增长", "净利润增速", "净利同比", "profit_growth"], "profit_growth"),
                        (["净资产收益率", "ROE", "roe"], "roe"),
                        (["资产负债率", "负债率", "debt_ratio"], "debt_ratio"),
                        (["每股收益", "EPS", "eps"], "eps"),
                        (["市盈率", "PE", "pe"], "pe"),
                        (["市净率", "PB", "pb"], "pb"),
                    ]:
                        for fn in field_names:
                            val = fin_data.get(fn)
                            if val is not None:
                                try:
                                    target[target_key] = float(str(val).replace("%", "").replace(",", "").strip())
                                    break
                                except (TypeError, ValueError):
                                    continue

                    # 是否有财报预告
                    for fn in ["业绩预告", "是否有业绩预告", "earnings_forecast"]:
                        val = fin_data.get(fn)
                        if val is not None:
                            target["has_earnings_forecast"] = str(val)
                            break

                    target["financial_raw"] = fin_data  # 保留原始数据
        except Exception as e:
            logger.debug("财务数据查询失败 %s: %s", code, e)

        # 4. 问财：主力资金净流入（需求 §10.6 资金数据）
        try:
            fund_result = self._skill.query("stock_quote", f"{code} 主力资金净流入")
            if fund_result and fund_result.get("data"):
                fund_data = fund_result["data"]
                if isinstance(fund_data, list) and fund_data:
                    fund_data = fund_data[0] if isinstance(fund_data[0], dict) else {}
                if isinstance(fund_data, dict):
                    for key in ("主力净流入", "主力资金净流入", "主力净流入额"):
                        val = fund_data.get(key)
                        if val is not None:
                            try:
                                target["main_fund_flow"] = float(val)
                            except (TypeError, ValueError):
                                pass
                            break
        except Exception as e:
            logger.debug("资金流向查询失败 %s: %s", code, e)

        # 5. 问财：事件数据 → 排雷（需求 §10.6 排雷）
        try:
            events = self._skill.query_events(code)
            if events and events.get("data"):
                events_data = events["data"]
                if isinstance(events_data, list) and events_data:
                    events_data = events_data[0] if isinstance(events_data[0], dict) else {}
                if isinstance(events_data, dict):
                    target["events"] = events_data

                    # 排雷标记
                    event_str = str(events_data)
                    if any(kw in event_str for kw in ["立案", "行政处罚", "退市"]):
                        target["risk_flag"] = "高风险"
                    elif any(kw in event_str for kw in ["减持", "质押", "解禁"]):
                        target["risk_flag"] = "中等风险"
                    else:
                        target["risk_flag"] = "无明显风险"
        except Exception as e:
            logger.debug("事件查询失败 %s: %s", code, e)

        # 6. 板块归属（同花顺行业）
        sector = target.get("sector", "")
        if sector:
            sw_code = normalize_sector(sector)
            if sw_code:
                target["sw_code"] = sw_code

        # 保存画像快照
        target["profile_json"] = json.dumps(target, ensure_ascii=False, default=str)
        logger.info("标的画像补全 %s: 价格=%s, MA=%s, 营收增速=%s, ROE=%s, 排雷=%s",
                    code,
                    target.get("current_price"),
                    target.get("ma_alignment"),
                    target.get("revenue_growth"),
                    target.get("roe"),
                    target.get("risk_flag", "未检查"))

    def _generate_verify_config(self, judgment_data: Dict, targets: List[Dict]) -> Dict:
        """生成验证配置"""
        direction = judgment_data.get("direction", "")
        tags = judgment_data.get("tags", [])

        # 简化：根据方向和标签生成基本的验证配置
        metrics = []

        if "涨价" in str(tags) or "涨价" in judgment_data.get("judgment", ""):
            metrics.append({
                "type": "sector_change_3d",
                "sector": "相关板块",
                "confirm_threshold": 0.03,
                "refute_threshold": -0.03,
            })
            metrics.append({
                "type": "sector_fund_flow_3d",
                "sector": "相关板块",
                "confirm_threshold": 500000000,
                "refute_threshold": -300000000,
            })

        if "中报" in str(tags) or "业绩" in str(tags):
            metrics.append({
                "type": "sector_fund_flow_3d",
                "sector": "相关板块",
                "confirm_threshold": 500000000,
                "refute_threshold": -300000000,
            })

        # 通用指标
        if not metrics:
            metrics.append({
                "type": "sector_change_3d",
                "sector": "相关板块",
                "confirm_threshold": 0.03,
                "refute_threshold": -0.03,
            })

        return {
            "metrics": metrics,
            "confirm_logic": "all",
            "refute_logic": "any",
        }

    def _fetch_metric_value(self, metric_type: str, targets: List[Dict], metric_cfg: Dict) -> Optional[Any]:
        """获取追踪指标值"""
        # 根据指标类型从数据源获取
        if metric_type == "sector_change_3d":
            sector = metric_cfg.get("sector", "")
            result = self._skill.query_sector(sector)
            if result and result.get("data"):
                return result["data"].get("change_3d")

        elif metric_type == "sector_fund_flow_3d":
            sector = metric_cfg.get("sector", "")
            result = self._skill.query_sector(sector)
            if result and result.get("data"):
                return result["data"].get("fund_flow_3d")

        return None

    def _check_threshold(self, value: Any, threshold: Any, check_type: str) -> bool:
        """检查指标是否达到阈值"""
        try:
            if check_type == "confirm":
                if isinstance(threshold, (int, float)):
                    return float(value) >= float(threshold)
            elif check_type == "refute":
                if isinstance(threshold, (int, float)):
                    return float(value) <= float(threshold)
        except (ValueError, TypeError):
            return False
        return False

    def _handle_chain_linkage(self, confirmed_judgment_id: str):
        """
        判断链联动处理
        上游判断兑现时，主动推送下游判断的预期强化
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # 查找下游判断
            cursor.execute("""
                SELECT jc.*, j.judgment as downstream_judgment, j.id as downstream_id
                FROM judgment_chains jc
                JOIN judgments j ON jc.downstream_judgment_id = j.id
                WHERE jc.upstream_judgment_id = ?
            """, (confirmed_judgment_id,))

            chains = cursor.fetchall()

            for chain in chains:
                downstream_id = chain["downstream_id"]
                downstream_text = chain["downstream_judgment"]
                logic = chain["logic"]

                # 推送联动消息
                self._pushplus.send(
                    title="🔗【判断链联动】",
                    content=f"""
                    <b>🔗 判断链联动</b><br/>
                    <br/>
                    ✅ 上游判断已兑现<br/>
                    🔗 下游判断预期强化：{downstream_text}<br/>
                    📊 关联逻辑：{logic}<br/>
                    <br/>
                    💡 建议关注下游判断的相关标的
                    """,
                    level="重要",
                )

                logger.info("判断链联动: %s → %s (%s)", confirmed_judgment_id, downstream_id, logic)

        except Exception as e:
            logger.error("判断链联动处理失败: %s", e)
        finally:
            conn.close()

    def _suggest_to_watchlist(self, stock_code: str):
        """
        建议将标的加入自选C类
        仅标记，不自动添加（需用户确认）
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE judgment_targets SET suggested_to_watchlist = TRUE WHERE stock_code = ?",
                (stock_code,),
            )
            conn.commit()
        except Exception as e:
            logger.error("标记自选建议失败: %s", e)
        finally:
            conn.close()

    def _row_to_judgment(self, row) -> Judgment:
        """数据库行转Judgment对象"""
        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except json.JSONDecodeError:
                tags = []

        verify_config = {}
        if row["verify_config"]:
            try:
                verify_config = json.loads(row["verify_config"])
            except json.JSONDecodeError:
                verify_config = {}

        # 获取关联标的
        targets = self._get_judgment_targets(row["id"])

        return Judgment(
            id=row["id"],
            insight_id=row["insight_id"],
            judgment=row["judgment"],
            direction=row["direction"],
            time_horizon=row["time_horizon"],
            confidence=row["confidence"],
            valid_days=row["valid_days"] or 30,
            expire_at=row["expire_at"] or "",
            status=row["status"],
            refute_count=row["refute_count"] or 0,
            tags=tags,
            targets=targets,
            verify_config=verify_config,
        )

    def _get_judgment_targets(self, judgment_id: str) -> List[Dict]:
        """获取判断关联标的"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT stock_code, stock_name, role FROM judgment_targets WHERE judgment_id = ?",
                (judgment_id,),
            )
            rows = cursor.fetchall()
            return [
                {"code": r["stock_code"], "name": r["stock_name"], "role": r["role"]}
                for r in rows
            ]
        except Exception as e:
            logger.error("获取判断标的失败: %s", e)
            return []
        finally:
            conn.close()

    def _save_insight(self, insight_id: str, source: str, raw_text: str, judgments: List[Dict], chains: List[Dict]):
        """保存观点到数据库"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = datetime.now().strftime("%Y-%m-%d")

            # 保存观点主表
            cursor.execute(
                "INSERT OR REPLACE INTO insights (id, source, raw_text, created_at, status, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (insight_id, source, raw_text[:2000], today, "tracking", today),
            )

            # 保存判断
            for j in judgments:
                j_id = j.get("id", f"J-{datetime.now().strftime('%H%M%S')}")
                expire_at = (datetime.now() + timedelta(days=j.get("valid_days", 30))).strftime("%Y-%m-%d")

                cursor.execute(
                    """INSERT OR REPLACE INTO judgments
                    (id, insight_id, judgment, direction, time_horizon, confidence,
                     valid_days, expire_at, status, refute_count, tags, verify_config, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        j_id, insight_id, j.get("judgment", ""),
                        j.get("direction", ""), j.get("time_horizon", ""),
                        j.get("confidence", ""), j.get("valid_days", 30),
                        expire_at, "tracking", 0,
                        json.dumps(j.get("tags", []), ensure_ascii=False),
                        json.dumps(j.get("verify_config", {}), ensure_ascii=False),
                        today, today,
                    ),
                )

                # 保存关联标的
                for target in j.get("targets", []):
                    cursor.execute(
                        """INSERT INTO judgment_targets (judgment_id, stock_code, stock_name, role, profile_json, suggested_to_watchlist, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            j_id, target.get("code", ""), target.get("name", ""),
                            target.get("role", ""),
                            target.get("profile_json", ""),
                            False, today,
                        ),
                    )

            conn.commit()
            logger.info("观点保存成功: %s", insight_id)
        except Exception as e:
            logger.error("保存观点失败: %s", e)
        finally:
            conn.close()

    def _save_chain(self, chain: Dict):
        """保存判断关联链"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO judgment_chains (upstream_judgment_id, downstream_judgment_id, logic) VALUES (?, ?, ?)",
                (chain.get("upstream", ""), chain.get("downstream", ""), chain.get("logic", "")),
            )
            conn.commit()
        except Exception as e:
            logger.error("保存判断链失败: %s", e)
        finally:
            conn.close()

    def _update_judgment_status(self, judgment_id: str, status: str, refute_count: Optional[int] = None):
        """更新判断状态"""
        conn = get_connection()
        try:
            cursor = conn.cursor()
            today = datetime.now().strftime("%Y-%m-%d")

            if refute_count is not None:
                cursor.execute(
                    "UPDATE judgments SET status = ?, refute_count = ?, updated_at = ? WHERE id = ?",
                    (status, refute_count, today, judgment_id),
                )
            else:
                cursor.execute(
                    "UPDATE judgments SET status = ?, updated_at = ? WHERE id = ?",
                    (status, today, judgment_id),
                )
            conn.commit()
        except Exception as e:
            logger.error("更新判断状态失败: %s", e)
        finally:
            conn.close()

    def _push_track_result(self, result: InsightTrackResult, source: str):
        """推送追踪结果"""
        signal_data = {
            "type": result.push_type,
            "judgment": result.judgment_text,
            "source": source,
            "targets": [{"name": t} for t in result.suggested_targets] if result.suggested_targets else [],
            "track_details": str(result.changes),
            "suggestion": "建议加入自选C类" if result.push_type == "confirming" else "",
        }

        title, content = render_insight_signal(signal_data)
        self._pushplus.send(title, content, level="重要")

    def get_weekly_summary(self) -> Dict:
        """
        获取观点挖掘周报摘要

        Returns:
            {
                "confirming": [...],  # 兑现中
                "tracking": [...],    # 追踪中
                "refuted": [...],     # 已证伪
                "expired": [...],     # 已过期
                "confirm_rate": float # 兑现率
            }
        """
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # 最近7天
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

            cursor.execute(
                "SELECT status, COUNT(*) as cnt FROM judgments WHERE created_at >= ? GROUP BY status",
                (week_ago,),
            )
            status_counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

            # 获取各状态判断详情
            summary = {
                "confirming": [],
                "tracking": [],
                "refuted": [],
                "expired": [],
            }

            for status in ["confirming", "tracking", "refuted", "expired"]:
                cursor.execute(
                    "SELECT j.id, j.judgment, j.direction FROM judgments j WHERE j.status = ? AND j.created_at >= ? ORDER BY j.updated_at DESC LIMIT 10",
                    (status, week_ago),
                )
                for row in cursor.fetchall():
                    summary[status].append({
                        "id": row["id"],
                        "judgment": row["judgment"],
                        "direction": row["direction"],
                    })

            # 兑现率
            total = sum(status_counts.values())
            confirmed = status_counts.get("confirming", 0)
            summary["confirm_rate"] = confirmed / total if total > 0 else 0

            return summary

        except Exception as e:
            logger.error("获取周报摘要失败: %s", e)
            return {"confirming": [], "tracking": [], "refuted": [], "expired": [], "confirm_rate": 0}
        finally:
            conn.close()


# 单例
_instance: Optional[InsightMiner] = None


def get_insight_miner() -> InsightMiner:
    global _instance
    if _instance is None:
        _instance = InsightMiner()
    return _instance
