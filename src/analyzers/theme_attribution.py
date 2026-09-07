"""
主题归属修正层（Phase2-B）— 行业数据库映射 ≠ 市场主题交易
============================================================

问题（用户实测批评）：
  板块归属按行业数据库自动映射：澜起/海光/大普微/中科飞测/芯碁微装/胜蓝/
  兆易创新 → "电子化学品"；骄成超声 → "电池"；创世纪 → "自动化设备"。
  而板块状态机（主线/退潮/轮动）直接决定闸门输出（退潮禁新仓/禁追强）——
  地基歪了，上层策略的精确性要打折扣。澜起被"电子化学品"的退潮状态误杀，
  或被"电池"的轮动状态放行，都不是它真实的交易语境（存储主线）。

方案：
  主题映射优先、行业兜底：
    ① config/theme_map.yaml overrides（人工映射，覆盖已知错配）
    ② 无映射 → 保持原行业结果（兜底，行为不变）
  状态判定：
    主题定义 proxy_boards（THS 行业名列表）→ 取最严格状态
    （retreating > main_trend > rotational > unknown）；
    代理状态不可得 → 沿用原行业状态。
  展示：
    显示名后缀"(主题)"，推送/日志可区分"归属被修正过"；修正记录
    （原归属 → 新归属）由调用方收集进调度摘要。

设计原则：本层只修正"归属"，不发明"状态"——状态仍来自板块真实行情
（快照/当日排名），避免人工映射变成拍脑袋状态机。
"""
import logging
import threading
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 最严格标签优先（与 sector_ranker/_classify_from_snapshot 语义一致：
# 一只股票挂多个板块时，退潮 > 主线 > 轮动）
_STATUS_PRIORITY = {"retreating": 3, "main_trend": 2, "rotational": 1, "unknown": 0}

_theme_map_cache: Optional[Dict] = None
_theme_map_ts: float = 0.0
_lock = threading.Lock()

BoardStatusFn = Callable[[str], Optional[str]]  # board_name -> classification


def _load_theme_map(refresh: bool = False) -> Dict:
    """加载 config/theme_map.yaml（进程内缓存 5 分钟）。"""
    global _theme_map_cache, _theme_map_ts
    import time
    now = time.time()
    with _lock:
        if _theme_map_cache is not None and not refresh and now - _theme_map_ts < 300:
            return _theme_map_cache
    data: Dict = {"enabled": False, "themes": {}, "overrides": {}}
    try:
        from pathlib import Path
        import yaml
        path = Path(__file__).parent.parent.parent / "config" / "theme_map.yaml"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            data["enabled"] = bool(loaded.get("enabled", True))
            data["themes"] = loaded.get("themes") or {}
            data["overrides"] = {str(k).zfill(6): str(v) for k, v in (loaded.get("overrides") or {}).items()}
    except Exception as e:
        logger.warning("主题映射表加载失败（按原行业链路）: %s", str(e)[:80])
    with _lock:
        _theme_map_cache = data
        _theme_map_ts = now
    return data


def _strictest_status(statuses: List[Optional[str]]) -> Optional[str]:
    """多代理板块状态取最严格：retreating > main_trend > rotational > unknown。"""
    valid = [s for s in statuses if s]
    if not valid:
        return None
    return max(valid, key=lambda s: _STATUS_PRIORITY.get(s, 0))


def resolve_stock_theme(
    code: str,
    name: str,
    fallback_sector: str = "",
    fallback_status: str = "",
    board_status_fn: Optional[BoardStatusFn] = None,
) -> Dict:
    """
    解析个股主题归属。

    Args:
        code: 股票代码
        name: 股票名称
        fallback_sector: 原行业链路板块名（sector_ranker 结果）
        fallback_status: 原行业链路板块状态
        board_status_fn: 板块名 → 状态（main_trend/rotational/retreating）查询函数

    Returns:
        {
          "theme": str,          # 主题键名（fallback 时为 ""）
          "display": str,        # 展示名（主题带"(主题)"后缀）
          "status": str,         # 闸门用状态（主题代理最严格 或 原状态）
          "status_source": str,  # theme_proxy / fallback
          "matched_by": str,     # override / fallback
          "original_sector": str,
          "original_status": str,
          "remapped": bool,      # 归属被修正（含状态跟随变化）
          "evidence": str,       # 人读证据（用于日志/推送）
        }
    """
    code = str(code).zfill(6)
    tm = _load_theme_map()
    result = {
        "theme": "",
        "display": str(fallback_sector or ""),
        "status": str(fallback_status or "rotational"),
        "status_source": "fallback",
        "matched_by": "fallback",
        "original_sector": str(fallback_sector or ""),
        "original_status": str(fallback_status or ""),
        "remapped": False,
        "evidence": "",
    }
    if not tm.get("enabled"):
        return result

    theme_key = (tm.get("overrides") or {}).get(code)
    if not theme_key:
        return result

    theme_def = (tm.get("themes") or {}).get(theme_key) or {}
    display = str(theme_def.get("display") or theme_key)

    # 状态：代理板块最严格 → 原状态兜底
    status = ""
    status_source = "fallback"
    proxy_boards = [str(b) for b in (theme_def.get("proxy_boards") or []) if str(b)]
    if board_status_fn is not None and proxy_boards:
        try:
            statuses = [board_status_fn(b) for b in proxy_boards]
            status = _strictest_status(statuses) or ""
            if status:
                status_source = "theme_proxy"
        except Exception as e:
            logger.debug("主题代理板块状态查询失败 %s: %s", theme_key, str(e)[:60])
            status = ""
    if not status:
        status = str(fallback_status or "rotational")

    result.update({
        "theme": theme_key,
        "display": display,
        "status": status,
        "status_source": status_source,
        "matched_by": "override",
        "remapped": True,
        "evidence": (
            f"{code} {name or ''}: {fallback_sector or '(未归类)'}→{display}(主题映射)"
            + (f"，状态[{status}@{status_source}，代理{'/'.join(proxy_boards)}]" if proxy_boards else "")
        ),
    })
    return result


def make_board_status_lookup(sector_map: Optional[Dict[str, str]] = None,
                             use_ranking: bool = True,
                             use_kline: bool = False) -> BoardStatusFn:
    """
    构建板块状态查询函数（供 resolve_stock_theme 的 board_status_fn）。

    优先级：sector_map（当日板块扫描结果）→ 东财当日排名（内存缓存）
    → THS K线轻量分类（可选，较慢，默认关闭）。
    """
    def lookup(board_name: str) -> Optional[str]:
        if not board_name:
            return None
        # 1. 调用方传入的板块名 → 状态映射
        if sector_map:
            for key in (board_name, board_name.replace("(主题)", "")):
                if key in sector_map:
                    return str(sector_map.get(key) or "") or None
        # 2. 东财当日排名（sector_ranker 内存缓存）
        if use_ranking:
            try:
                from .sector_ranker import _refresh_daily_ranking
                ranking = _refresh_daily_ranking()
                if ranking:
                    info = ranking.get(board_name)
                    if not info:
                        for rname, rinfo in ranking.items():
                            if len(board_name) >= 2 and (board_name in rname or rname in board_name):
                                info = rinfo
                                break
                    if info:
                        return str(info.get("classification") or "") or None
            except Exception:
                pass
        # 3. K线轻量分类（兜底，较慢）
        if use_kline:
            try:
                from ..cache.builders import compute_sector_metrics_from_kline, classify_sector_status
                metrics = compute_sector_metrics_from_kline(board_name)
                if metrics:
                    return str(classify_sector_status(metrics) or "") or None
            except Exception:
                pass
        return None

    return lookup


def apply_theme_attribution(
    stock_sector: Dict[str, str],
    stock_sector_status: Dict[str, str],
    name_by_code: Optional[Dict[str, str]] = None,
    board_status_fn: Optional[BoardStatusFn] = None,
) -> Dict:
    """
    批量应用主题归属修正（原地修改 stock_sector / stock_sector_status）。

    Args:
        stock_sector: code → 板块名（将被改写为主题展示名）
        stock_sector_status: code → 状态（将被改写为主题代理状态）
        name_by_code: code → 股票名
        board_status_fn: 板块状态查询函数

    Returns:
        {"remaps": [resolve_stock_theme 结果...]，"hit": int}
    """
    name_by_code = name_by_code or {}
    remaps: List[Dict] = []
    for code in list(stock_sector.keys()):
        attr = resolve_stock_theme(
            code=code,
            name=name_by_code.get(code, ""),
            fallback_sector=stock_sector.get(code, ""),
            fallback_status=stock_sector_status.get(code, ""),
            board_status_fn=board_status_fn,
        )
        if not attr.get("remapped"):
            continue
        stock_sector[code] = attr["display"]
        stock_sector_status[code] = attr["status"]
        remaps.append(attr)
        logger.info("主题归属修正: %s", attr.get("evidence"))
    if remaps:
        logger.info("主题归属修正完成: %d/%d 只（其余沿用行业链路）", len(remaps), len(stock_sector))
    return {"remaps": remaps, "hit": len(remaps)}


def reset_theme_state() -> None:
    """清空映射表缓存（测试用）。"""
    global _theme_map_cache, _theme_map_ts
    with _lock:
        _theme_map_cache = None
        _theme_map_ts = 0.0
