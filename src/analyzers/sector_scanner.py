"""
板块扫描引擎 - 第二层决策
三级量化分类 + 交叉诊断

修复#5：量化三级分类标准，替代定性描述
"""
import logging
from typing import Tuple, Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from ..config_models import load_config
from ..db import get_connection
from ..data_layer.akshare_adapter import get_akshare_adapter
from ..data_layer.skill_wrapper import get_skill_wrapper
from ..data_layer.data_cache import get_data_cache
from ..data_layer.sw_industry import normalize_sector, calc_sector_metrics, SW_LEVEL1, SW_LEVEL2

logger = logging.getLogger(__name__)


class SectorClassification(str, Enum):
    """板块三级分类"""
    MAIN_TREND = "main_trend"    # 主线热点
    ROTATIONAL = "rotational"    # 支线轮动
    RETREATING = "retreating"    # 退潮
    UNKNOWN = "unknown"          # 数据不足


@dataclass
class SectorResult:
    """单个板块扫描结果"""
    name: str
    classification: SectorClassification
    classification_name: str        # 中文名称
    conditions_met: List[str]       # 满足的条件描述
    conditions_detail: Dict[str, Any] = field(default_factory=dict)  # 各指标原始值
    score: float = 0.0             # 匹配得分（满足条件比例）


@dataclass
class CrossDiagnosisResult:
    """交叉诊断结果"""
    stock_code: str
    stock_name: str
    sector_name: str
    sector_classification: SectorClassification
    market_mode: str               # attack/defend/retreat
    action: str                    # hold_or_add / hold_observe / reduce_signal / clear_signal_enhanced / etc.
    action_desc: str = ""          # 行为描述


@dataclass
class SectorScanResult:
    """板块扫描完整结果"""
    date: str
    sectors: List[SectorResult] = field(default_factory=list)
    cross_diagnosis: List[CrossDiagnosisResult] = field(default_factory=list)
    main_trend_sectors: List[str] = field(default_factory=list)
    rotational_sectors: List[str] = field(default_factory=list)
    retreating_sectors: List[str] = field(default_factory=list)
    sector_ranks: Dict[str, dict] = field(default_factory=dict)  # {???: {rank, total, change_3d}}


class SectorScanner:
    """板块扫描引擎（增强版：行业轮动 + 资金流）"""

    # 轮动四阶段
    ROTATION_PHASES = {
        "lead":    {"name": "龙头启动",   "desc": "龙头股先涨，板块效应酝酿中"},
        "confirm": {"name": "中军确认",   "desc": "权重股拉升，板块趋势确立"},
        "chase":   {"name": "跟风补涨",   "desc": "后排补涨，板块进入高潮"},
        "exit":    {"name": "资金撤离",   "desc": "龙头提前走弱，资金切换"},
    }

    def __init__(self):
        self._config = load_config("sector_scanner.yaml")
        self._classification_cfg = self._config.get("classification", {})
        self._cross_diagnosis_cfg = self._config.get("cross_diagnosis", {})
        self._akshare = get_akshare_adapter()
        self._skill = get_skill_wrapper()
        self._cache = get_data_cache()
        # 板块数据批量预取缓存：{sector_name: metrics_dict}
        # 在 scan() 入口由 _batch_fetch_sector_data 一次性填充，供 _fetch_sector_data 复用
        self._sector_data_cache: Optional[Dict[str, Dict[str, Any]]] = None

    def scan(self, target_sectors: Optional[List[str]] = None, market_mode: str = "defend") -> SectorScanResult:
        """
        执行板块扫描

        Args:
            target_sectors: 要扫描的板块列表，None则扫描所有活跃板块
            market_mode: 当前操作模式

        Returns:
            SectorScanResult
        """
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info("=== 板块扫描开始 %s ===", today)

        result = SectorScanResult(date=today)

        # 1. 获取待扫描板块列表
        if target_sectors is None:
            target_sectors, sector_ranks = self._get_active_sectors()
            result.sector_ranks = sector_ranks

        logger.info("待扫描板块: %s", target_sectors)

        # 1.1 批量预取板块数据，填充 _sector_data_cache，供 _fetch_sector_data 复用
        self._sector_data_cache = self._batch_fetch_sector_data(target_sectors)

        # 2. 逐板块分类
        for sector_name in target_sectors:
            sector_result = self._classify_sector(sector_name)
            result.sectors.append(sector_result)

            if sector_result.classification == SectorClassification.MAIN_TREND:
                result.main_trend_sectors.append(sector_name)
            elif sector_result.classification == SectorClassification.ROTATIONAL:
                result.rotational_sectors.append(sector_name)
            elif sector_result.classification == SectorClassification.RETREATING:
                result.retreating_sectors.append(sector_name)

        # 3. 保存板块扫描结果
        self._save_scan_result(result)

        # 4. 分类汇总
        logger.info(
            "板块扫描完成: 主线%d / 支线%d / 退潮%d",
            len(result.main_trend_sectors),
            len(result.rotational_sectors),
            len(result.retreating_sectors),
        )

        return result

    def cross_diagnose(
        self,
        scan_result: SectorScanResult,
        holdings: List[Dict],
        watchlist: List[Dict],
        market_mode: str,
    ) -> List[CrossDiagnosisResult]:
        """
        交叉诊断：持仓板块 × 板块状态 × 操作模式

        Args:
            scan_result: 板块扫描结果
            holdings: 持仓列表 [{"code": "xxx", "name": "xxx", "sector": "半导体"}, ...]
            watchlist: 自选列表 [{"code": "xxx", "name": "xxx", "sector": "..."}, ...]
            market_mode: 操作模式

        Returns:
            交叉诊断结果列表
        """
        logger.info("=== 交叉诊断开始 ===")

        # 构建板块→分类映射
        sector_class_map = {}
        for sr in scan_result.sectors:
            sector_class_map[sr.name] = sr.classification

        diagnosis_results = []

        # 持仓交叉诊断
        for holding in holdings:
            sector_name = holding.get("sector", "")
            if not sector_name:
                continue

            classification = sector_class_map.get(sector_name)
            if classification is None:
                classification = self._fuzzy_match_sector(sector_name, sector_class_map)

            action, action_desc = self._get_cross_action(classification, market_mode, is_holding=True)

            diagnosis_results.append(CrossDiagnosisResult(
                stock_code=holding["code"],
                stock_name=holding["name"],
                sector_name=sector_name,
                sector_classification=classification or SectorClassification.UNKNOWN,
                market_mode=market_mode,
                action=action,
                action_desc=action_desc,
            ))

        # 自选交叉诊断
        for stock in (watchlist or []):
            if hasattr(stock, 'sector'):
                sector_name = getattr(stock, 'sector', '') or ''
                stock_code = getattr(stock, 'code', '') or ''
                stock_name = getattr(stock, 'name', '') or ''
            elif isinstance(stock, dict):
                sector_name = stock.get("sector", "")
                stock_code = stock.get("code", "")
                stock_name = stock.get("name", "")
            else:
                continue

            if not sector_name:
                continue

            classification = sector_class_map.get(sector_name)
            if classification is None:
                classification = self._fuzzy_match_sector(sector_name, sector_class_map)

            action, action_desc = self._get_cross_action(classification, market_mode, is_holding=False)

            diagnosis_results.append(CrossDiagnosisResult(
                stock_code=stock_code,
                stock_name=stock_name,
                sector_name=sector_name,
                sector_classification=classification or SectorClassification.UNKNOWN,
                market_mode=market_mode,
                action=action,
                action_desc=action_desc,
            ))

        scan_result.cross_diagnosis = diagnosis_results

        logger.info("交叉诊断完成: %d 条诊断结果", len(diagnosis_results))
        return diagnosis_results

    def _classify_sector(self, sector_name: str) -> SectorResult:
        """
        板块分类（基于 SW 行业指数真实均线数据，不虚构条件匹配）

        直接读 SW 行业指数的均线排列（MA5/MA10/MA20）和 MA20 位置：
          主线：均线多头排列(MA5>MA10>MA20) + 站上MA20
          退潮：均线空头排列(MA5<MA10<MA20) + 跌破MA20
          轮动：其他所有情况（交叉、单线偏离等）
        轮动阶段检测"资金撤离"信号优先覆盖。
        """
        sector_data = self._fetch_sector_data(sector_name)
        rotation = self.detect_rotation_phase(sector_name, sector_data)

        # 资金撤离（龙头走弱+资金流出）→ 退潮，优先于均线判定
        if rotation["phase"] == "exit" and rotation["confidence"] != "低":
            return SectorResult(
                name=sector_name,
                classification=SectorClassification.RETREATING,
                classification_name="退潮",
                conditions_met=[f"轮动:资金撤离({rotation['desc']})"],
                conditions_detail={**sector_data, "rotation": rotation},
                score=1.0,
            )

        # 直接读 SW 行业指数的均线状态
        ma_align = sector_data.get("ma_alignment", "cross")
        above_ma20 = sector_data.get("sector_above_ma20", False)

        if ma_align == "bullish" and above_ma20:
            classification = SectorClassification.MAIN_TREND
            class_name = "主线热点"
            conditions = [f"MA多头排列, 站上MA20", f"轮动:{rotation['phase_name']}"]
            score = 3.0
        elif ma_align == "bearish" and not above_ma20:
            classification = SectorClassification.RETREATING
            class_name = "退潮"
            conditions = [f"MA空头排列, 跌破MA20", f"轮动:{rotation['phase_name']}"]
            score = 1.0
        else:
            classification = SectorClassification.ROTATIONAL
            class_name = "轮动"
            conditions = [f"MA排列:{ma_align}, {'站上' if above_ma20 else '跌破'}MA20", f"轮动:{rotation['phase_name']}"]
            score = 2.0

        # 板块层面机构资金维度：复用已有 real_fund_flow（行业资金流，来自 stock_fund_flow_industry）
        # 不再遍历成分股（避免 30 只 × 4 API = 120 次调用触发反爬）
        # real_fund_flow > 0 = 机构资金净流入，< 0 = 机构资金净流出
        real_fund_flow = sector_data.get("real_fund_flow")
        if real_fund_flow is not None:
            if real_fund_flow > 500000000:  # 行业主力净流入 > 5 亿
                score += 0.3
                conditions.append(f"机构资金净流入({real_fund_flow/1e8:.1f}亿)")
            elif real_fund_flow < -500000000:  # 行业主力净流出 > 5 亿
                score -= 0.3
                conditions.append(f"机构资金净流出({real_fund_flow/1e8:.1f}亿)")

        return SectorResult(
            name=sector_name,
            classification=classification,
            classification_name=class_name,
            conditions_met=conditions,
            conditions_detail={**sector_data, "rotation": rotation},
            score=score,
        )

    def detect_rotation_phase(self, sector_name: str, sector_data: Dict) -> Dict:
        """
        检测行业轮动节奏（龙头启动 → 中军确认 → 跟风补涨 → 资金撤离）

        判断依据：
        - 板块内涨停数（limit_up_count）和内部热度（internal_heat）
        - 连续站稳MA5天数（consecutive_above_ma5）
        - 真实资金流方向（real_fund_flow）
        - 历史排名趋势（从DB查前N天的分类状态）

        Returns:
            {"phase": "lead"|"confirm"|"chase"|"exit", "phase_name": str, "confidence": str}
        """
        limit_up = sector_data.get("limit_up_count", 0) or 0
        internal_heat = sector_data.get("internal_heat", 0) or 0.0
        consecutive_days = sector_data.get("consecutive_above_ma5", 0) or 0
        real_fund_flow = sector_data.get("real_fund_flow")
        alignment = sector_data.get("ma_alignment", "cross")
        change_3d = sector_data.get("sector_change_3d", 0) or 0

        # 查询历史：昨天该板块的状态
        prev_classification = self._get_previous_classification(sector_name)

        # ── 资金撤离：MA走弱 + 资金流出 ──
        if real_fund_flow is not None and real_fund_flow < 0 and change_3d < 0:
            if prev_classification in ("main_trend", "rotational"):
                return {"phase": "exit", "phase_name": "资金撤离",
                        "confidence": "高", "desc": "资金流出 + 涨幅转负，前期热点退潮"}

        # ── 龙头启动：少数涨停 + 板块刚开始涨 ──
        if limit_up <= 3 and internal_heat < 0.05 and consecutive_days <= 2:
            if alignment == "bullish" or change_3d > 0.01:
                return {"phase": "lead", "phase_name": "龙头启动",
                        "confidence": "中", "desc": "少量个股涨停领涨，板块效应初现"}

        # ── 中军确认：涨停增多 + 站稳MA5 + 资金流入 ──
        if limit_up >= 3 and consecutive_days >= 3 and alignment == "bullish":
            fund_confirm = real_fund_flow is None or real_fund_flow > 0
            if fund_confirm:
                return {"phase": "confirm", "phase_name": "中军确认",
                        "confidence": "高", "desc": "权重启动 + 资金流入，板块趋势确立"}
            else:
                return {"phase": "confirm", "phase_name": "中军确认",
                        "confidence": "中", "desc": "技术面确认，但资金面有待验证"}

        # ── 跟风补涨：高热度 + 持续多日 ──
        if (internal_heat >= 0.08 or limit_up >= 5) and consecutive_days >= 5:
            return {"phase": "chase", "phase_name": "跟风补涨",
                    "confidence": "中" if real_fund_flow and real_fund_flow > 0 else "低",
                    "desc": "后排补涨加速，警惕龙头提前走弱"}

        # ── 兜底：根据MA趋势判断 ──
        if alignment == "bullish":
            return {"phase": "confirm", "phase_name": "中军确认",
                    "confidence": "低", "desc": "均线多头，默认趋势延续"}
        elif alignment == "bearish":
            return {"phase": "exit", "phase_name": "资金撤离",
                    "confidence": "低", "desc": "均线空头，趋势转弱"}

        return {"phase": "lead", "phase_name": "龙头启动",
                "confidence": "低", "desc": "数据不足，默认初期阶段"}

    def _get_previous_classification(self, sector_name: str) -> Optional[str]:
        """从DB查询该板块昨天的分类"""
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT classification FROM sector_scan_history WHERE date=? AND sector_name=? ORDER BY date DESC LIMIT 1",
                (yesterday, sector_name),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def _evaluate_classification(
        self,
        sector_name: str,
        sector_data: Dict,
        class_type: str,
        class_cfg: Dict,
    ) -> Dict:
        """
        评估板块是否满足某级分类条件

        Args:
            sector_name: 板块名称
            sector_data: 板块数据
            class_type: 分类类型
            class_cfg: 分类配置

        Returns:
            {"matched": bool, "conditions_met": [...], "conditions_detail": {...}, "score": float}
        """
        conditions = class_cfg.get("conditions", [])
        match_logic = class_cfg.get("match_logic", "at_least_2")

        # 解析 match_logic: "at_least_N" → 需要满足N个条件
        required_count = 2  # 默认
        if match_logic.startswith("at_least_"):
            try:
                required_count = int(match_logic.split("_")[-1])
            except ValueError:
                required_count = 2
        elif match_logic == "all":
            required_count = len(conditions)

        conditions_met = []
        conditions_detail = {}

        for cond in conditions:
            metric = cond.get("metric", "")
            operator = cond.get("operator", "")
            threshold = cond.get("threshold", 0)
            desc = cond.get("desc", f"{metric} {operator} {threshold}")

            # 获取指标值
            metric_value = sector_data.get(metric)
            conditions_detail[metric] = {
                "value": metric_value,
                "threshold": threshold,
                "operator": operator,
            }

            # 评估条件
            if metric_value is None:
                conditions_detail[metric]["met"] = False
                continue

            met = self._evaluate_metric(metric_value, operator, threshold)
            conditions_detail[metric]["met"] = met

            if met:
                conditions_met.append(desc)

        matched = len(conditions_met) >= required_count
        total_conditions = len(conditions)
        score = len(conditions_met) / total_conditions if total_conditions > 0 else 0

        return {
            "matched": matched,
            "conditions_met": conditions_met,
            "conditions_detail": conditions_detail,
            "score": score,
        }

    def _evaluate_metric(self, value: Any, operator: str, threshold: Any) -> bool:
        """
        评估单个指标条件

        Args:
            value: 实际值
            operator: 比较操作符
            threshold: 阈值

        Returns:
            条件是否满足
        """
        try:
            if operator == ">=":
                return float(value) >= float(threshold)
            elif operator == ">":
                return float(value) > float(threshold)
            elif operator == "<=":
                return float(value) <= float(threshold)
            elif operator == "<":
                return float(value) < float(threshold)
            elif operator == "==":
                if isinstance(threshold, bool):
                    return bool(value) == threshold
                return str(value) == str(threshold)
            elif operator == "between":
                # threshold 是 [min, max] 列表
                if isinstance(threshold, list) and len(threshold) == 2:
                    return float(threshold[0]) <= float(value) <= float(threshold[1])
            elif operator == "between_ma10_ma20":
                # 特殊：板块指数在MA10~MA20之间
                return value == "between_ma10_ma20"
        except (ValueError, TypeError):
            return False

        return False

    def _batch_fetch_sector_data(self, sector_names: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量预取板块数据，统一通过申万行业数据层（AKShare）拉取。

        使用线程池并行拉取，避免串行等待反爬熔断。
        """
        if not sector_names:
            return {}

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        cache: Dict[str, Dict[str, Any]] = {}
        cache_lock = threading.Lock()

        def _fetch_one(name: str) -> tuple:
            try:
                sw_code = normalize_sector(name)
                if sw_code:
                    metrics = calc_sector_metrics(sw_code)
                    return (name, metrics or {})
                else:
                    from ..data_layer.sw_industry import calc_concept_metrics
                    metrics = calc_concept_metrics(name)
                    return (name, metrics or {})
            except Exception as e:
                logger.warning("批量预取板块 '%s' 数据失败: %s", name, e)
                return (name, {})

        # 并行拉取（max_workers=4，避免触发反爬）
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_one, name): name for name in sector_names}
            for future in as_completed(futures, timeout=120):
                try:
                    name, metrics = future.result(timeout=30)
                    with cache_lock:
                        cache[name] = metrics
                except Exception as e:
                    name = futures[future]
                    logger.warning("板块 '%s' 拉取超时或失败: %s", name, e)
                    with cache_lock:
                        cache[name] = {}

        logger.info(
            "批量预取板块数据完成: %d 个板块，命中指标 %d 个",
            len(sector_names),
            sum(1 for v in cache.values() if v),
        )
        return cache

    def _fetch_sector_data(self, sector_name: str) -> Dict[str, Any]:
        """
        获取板块数据用于分类评估

        优先级：
        1. _sector_data_cache（scan() 入口批量预取的缓存）
        2. calc_sector_metrics(申万行业数据，AKShare 数据源)
        3. 失败返回空字典

        Returns:
            {
                "sector_change_3d": float,      # 3日涨跌幅
                "sector_change_5d": float,      # 5日涨跌幅
                "sector_fund_flow_5d": float,   # 5日资金净流入
                "limit_up_count": int,          # 板块内涨停股数
                "sector_above_ma20": bool,      # 板块指数是否站上MA20
                "sector_ma_position": str,      # MA位置关系
            }
        """
        # 方法1：优先使用批量预取缓存
        if self._sector_data_cache is not None:
            cached = self._sector_data_cache.get(sector_name)
            if cached is not None:
                logger.debug("板块 '%s' 命中批量预取缓存", sector_name)
                return cached

        # 方法2：缓存未命中 → 申万行业数据层，或概念板块数据层
        try:
            sw_code = normalize_sector(sector_name)
            if sw_code:
                metrics = calc_sector_metrics(sw_code)
                if metrics:
                    logger.debug("板块 '%s' 实时拉取申万指标成功: %s", sector_name, list(metrics.keys()))
                    return metrics

            # 非申万板块 → 尝试概念板块指标
            from ..data_layer.sw_industry import calc_concept_metrics
            metrics = calc_concept_metrics(sector_name)
            if metrics:
                logger.debug("板块 '%s' 实时拉取概念指标成功: %s", sector_name, list(metrics.keys()))
                return metrics
        except Exception as e:
            logger.warning("实时拉取板块 '%s' 指标失败: %s", sector_name, e)

        # 方法3：失败返回空字典
        logger.debug("板块 '%s' 数据采集失败，返回空字典", sector_name)
        return {}

    @staticmethod
    def _extract_float(data: Dict, field_names: List[str]) -> Optional[float]:
        """从字典中按多个可能的字段名提取浮点值"""
        for name in field_names:
            val = data.get(name)
            if val is not None:
                try:
                    # 处理百分号字符串如 "3.5%"
                    if isinstance(val, str):
                        val = val.replace("%", "").replace(",", "").strip()
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None

    def _get_active_sectors(self) -> Tuple[List[str], Dict[str, dict]]:
        """
        获取活跃板块列表（使用申万二级行业，约 113 个，更精细）。

        策略：遍历二级行业，调用 calc_sector_metrics 获取 3 日涨幅，
        按涨幅降序取前 15 返回板块名称。

        失败时降级到默认板块列表。
        """
        from ..data_layer.sw_industry import SW_LEVEL2
        scored: List[tuple] = []
        for sw_code, sw_name in SW_LEVEL2.items():
            try:
                metrics = calc_sector_metrics(sw_code)
                change_3d = metrics.get("sector_change_3d")
                if change_3d is None:
                    continue
                scored.append((sw_name, float(change_3d)))
            except Exception as e:
                logger.debug("申万二级板块 %s(%s) 失败: %s", sw_name, sw_code, e)

        if not scored:
            logger.warning("申万二级行业指标全部拉取失败，降级到默认活跃板块")
            return ["半导体", "消费电子", "电池", "光伏设备", "软件开发", "航空装备", "化学制药", "白酒"]

        scored.sort(key=lambda x: x[1], reverse=True)
        top15 = scored[:15]
        logger.info(
            "申万二级活跃板块 Top15 (按涨幅): %s",
            ", ".join(f"{n}({c*100:.2f}%)" for n, c in top15),
        )
        return [name for name, _ in top15]

    def _get_cross_action(
        self,
        classification: Optional[SectorClassification],
        market_mode: str,
        is_holding: bool,
    ) -> tuple:
        """
        根据板块分类和操作模式确定交叉诊断行为

        Returns:
            (action_key, action_desc)
        """
        if classification is None or classification == SectorClassification.UNKNOWN:
            return ("unknown", "板块数据不足，无法诊断")

        # 从交叉诊断矩阵中查找
        class_key = classification.value  # main_trend / rotational / retreating
        mode_actions = self._cross_diagnosis_cfg.get(class_key, {})
        action_key = mode_actions.get(market_mode, "observe")

        # 行为描述映射
        action_descriptions = {
            "hold_or_add": "板块主线且市场进攻 → 持有/可加仓，自选可推送入场",
            "hold_observe": "板块主线但市场保守 → 持有观察，不急于操作",
            "observe_arbitrage_only": "板块支线且市场进攻 → 仅套利低吸，不追强",
            "observe_no_push": "板块支线且市场防守 → 观察，不推送入场",
            "no_push": "板块支线/退潮且市场撤退 → 不推送入场",
            "reduce_signal": "板块退潮 → 推送减仓信号",
            "clear_signal_enhanced": "板块退潮且市场撤退 → 推送清仓信号（加强）",
        }

        desc = action_descriptions.get(action_key, action_key)

        # 持仓和自选的差异化描述
        if is_holding:
            if action_key in ("reduce_signal", "clear_signal_enhanced"):
                desc = f"[持仓] {desc}"
            elif action_key == "hold_or_add":
                desc = f"[持仓] {desc}"
        else:
            if action_key in ("hold_or_add", "observe_arbitrage_only"):
                desc = f"[自选] 可推送入场信号"
            elif action_key in ("observe_no_push", "no_push"):
                desc = f"[自选] 不推送入场信号"
            elif action_key in ("reduce_signal", "clear_signal_enhanced"):
                desc = f"[自选] 所在板块退潮，暂不关注"

        return (action_key, desc)

    def _fuzzy_match_sector(self, sector_name: str, sector_map: Dict[str, SectorClassification]) -> Optional[SectorClassification]:
        """
        模糊匹配板块名称

        当持仓/自选中的板块名称与扫描结果不完全一致时进行模糊匹配
        """
        # 精确匹配
        if sector_name in sector_map:
            return sector_map[sector_name]

        # 包含匹配
        for scanned_name, classification in sector_map.items():
            if sector_name in scanned_name or scanned_name in sector_name:
                return classification

        # 关键词匹配
        keywords_map = {
            "半导体": ["芯片", "集成电路", "封测", "存储", "GPU", "CPU"],
            "新能源": ["光伏", "锂电", "风电", "储能", "充电桩"],
            "人工智能": ["AI", "大模型", "算力", "智算"],
            "军工": ["航天", "航空", "兵器", "船舶"],
            "医药": ["创新药", "CXO", "医疗器械", "中药"],
            "消费电子": ["苹果链", "VR", "MR", "手机"],
        }

        for main_sector, aliases in keywords_map.items():
            if sector_name in aliases or any(a in sector_name for a in aliases):
                if main_sector in sector_map:
                    return sector_map[main_sector]

        return None

    def _save_scan_result(self, result: SectorScanResult):
        """保存板块扫描结果到数据库"""
        import json
        conn = get_connection()
        try:
            cursor = conn.cursor()
            for sector in result.sectors:
                details = {
                    "conditions_met": sector.conditions_met,
                    "score": sector.score,
                    "rotation": sector.conditions_detail.get("rotation", {}),
                }
                cursor.execute(
                    """
                    INSERT INTO sector_scan_history (date, sector_name, classification, details)
                    VALUES (?, ?, ?, ?)
                    """,
                    (result.date, sector.name, sector.classification.value, json.dumps(details, ensure_ascii=False)),
                )
            conn.commit()
        except Exception as e:
            logger.error("Failed to save sector scan result: %s", e)
        finally:
            conn.close()


# 单例
_instance: Optional[SectorScanner] = None


def get_sector_scanner() -> SectorScanner:
    global _instance
    if _instance is None:
        _instance = SectorScanner()
    return _instance
