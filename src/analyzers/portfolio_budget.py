"""
组合预算（决策记录 P2-3）— 个股纪律齐备，集群不能裸奔
=====================================================

依据：
  9/7 观察区 23/24 主线、半导体设备 8 只同标签——个股层面 Z 线/置信度/
  基本面闸门齐备，组合层面同板块敞口无任何约束。一旦板块状态机误判
  （"主线"持续误判），全部个股纪律同向失效，组合承受相关性坍塌。

方案：
  新入场信号按板块（主题标签）计数：已有持仓 + 已放行新信号同板块
  数量 ≥ max_per_sector 时，该板块后续新信号降级为观察并留痕
  （集群风险拦截，非个股否决——个股纪律结论保留，审计可见）。

作废条件：若板块分散度回测显示集中持有主线反而期望值更高，
放宽上限而非删除。
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "enabled": True,
    "max_per_sector": 3,       # 同板块（主题标签）最大并发敞口数（持仓+新信号）
    "include_holdings": True,  # 预算计入已有持仓
}


def _sector_key(sector_name: str) -> str:
    return str(sector_name or "").replace("(主题)", "").strip()


def apply_portfolio_budget(
    entries: List,
    holdings: Optional[List[Dict]] = None,
    config: Optional[Dict] = None,
    sector_of_entry=None,
) -> Dict:
    """对新入场信号施加组合预算（同板块敞口上限）。

    Args:
        entries: EntrySignal 列表（将原地降级超额信号）
        holdings: 已有持仓 [{stock_code, ...}]（预算基数）
        config: portfolio_budget 配置块
        sector_of_entry: callable(sig) -> 板块名（缺省读 sig.sector_name）

    Returns:
        {"applied": bool, "blocked": [{code, name, sector, reason, ...}],
         "counts": {板块: 数量}, "note": str}
    """
    cfg = dict(DEFAULT_CONFIG)
    if config and isinstance(config.get("portfolio_budget"), dict):
        cfg.update({k: v for k, v in config["portfolio_budget"].items() if v is not None})

    if not cfg.get("enabled", True):
        return {
            "applied": False, "blocked": [], "counts": {},
            "note": "组合预算关闭", "passed": list(entries or []),
        }

    max_per_sector = int(cfg.get("max_per_sector", 3))
    get_sector = sector_of_entry or (lambda sig: getattr(sig, "sector_name", "") or "")

    counts: Dict[str, int] = {}
    if cfg.get("include_holdings", True):
        for h in holdings or []:
            sector = _sector_key(h.get("sector_name") or "")
            if sector:
                counts[sector] = counts.get(sector, 0) + 1

    blocked: List[Dict] = []
    passed: List = []
    for sig in entries or []:
        sector = _sector_key(get_sector(sig))
        if not sector:
            passed.append(sig)
            continue
        current = counts.get(sector, 0)
        if current >= max_per_sector:
            blocked.append({
                "stock_code": getattr(sig, "stock_code", ""),
                "stock_name": getattr(sig, "stock_name", ""),
                "entry_type": getattr(sig, "entry_type", ""),
                "sector": sector,
                "reason": (
                    f"组合预算:板块[{sector}]并发敞口已达{current}/{max_per_sector}"
                    "（含持仓与已放行信号），集群风险拦截——个股纪律结论保留，"
                    "降级为观察（作废条件：若回测显示集中主线期望值更高，放宽上限而非删除）"
                ),
                "benchmark_price": getattr(sig, "benchmark_price", 0),
                "stop_loss": getattr(sig, "stop_loss", 0),
                "target_range": list(getattr(sig, "target_range", []) or []),
                "hypothesis": getattr(sig, "hypothesis", {}) or {},
                "fundamental": None,
                "fundamental_rejected": False,
                "valuation": None,
                "valuation_rejected": False,
            })
            continue
        counts[sector] = current + 1
        passed.append(sig)

    note = ""
    if blocked:
        note = (
            f"组合预算拦截{len(blocked)}条同板块新信号"
            f"（上限{max_per_sector}/板块，含持仓计票）"
        )
    return {
        "applied": True,
        "blocked": blocked,
        "counts": counts,
        "note": note,
        "passed": passed,
    }
