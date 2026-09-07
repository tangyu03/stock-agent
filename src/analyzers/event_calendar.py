"""
F5: 事件日历
============
数据源：
  1. ak.stock_restricted_release_summary_em — 解禁日历（全市场）
  2. ak.stock_restricted_release_queue_em — 解禁队列（个股）
  3. 财报节点：通过 config/event_calendar.yaml 人工维护（鸡哥财报前两日谨慎等）

事件类型：
  - 解禁：解禁日前5日/前1日/当日 减仓预警
  - 财报：财报预告日前2日 谨慎（防业绩雷）
  - 停战期满/宏观事件：人工标注（降级，无稳定API）

输出：
  {
    "events": [
      {"date": "2024-08-15", "type": "解禁", "code": "688008", "detail": "解禁市值10亿，占流通5%"},
      ...
    ],
    "warning_level": "高/中/低/无",
  }

使用方式：
  from src.analyzers.event_calendar import get_upcoming_events, check_event_warning
  events = get_upcoming_events(stock_code="688008", days=30)
  warning = check_event_warning(stock_code="688008")
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# session 缓存
_unlock_summary_cache: Optional[Any] = None
_unlock_cache_date: str = ""


def _load_unlock_summary():
    """加载解禁日历（session缓存）"""
    global _unlock_summary_cache, _unlock_cache_date
    today = datetime.now().strftime("%Y%m%d")
    if _unlock_cache_date != today:
        _unlock_summary_cache = None
        _unlock_cache_date = today
    if _unlock_summary_cache is None:
        try:
            # P0-6: 改用 safe_ak_func 带超时保护
            from ..data_layer.akshare_safe import safe_ak_func
            release_summary = safe_ak_func("stock_restricted_release_summary_em", timeout=30)
            _unlock_summary_cache = release_summary()
            logger.info("解禁日历加载: %d行", len(_unlock_summary_cache) if _unlock_summary_cache is not None else 0)
        except Exception as e:
            logger.warning("解禁日历加载失败: %s", e)
            _unlock_summary_cache = None
    return _unlock_summary_cache


def _load_manual_calendar() -> List[Dict]:
    """加载人工维护的事件日历（财报节点/停战期满/宏观事件）"""
    cal_path = Path(__file__).parent.parent.parent / "config" / "event_calendar.yaml"
    if not cal_path.exists():
        return []
    try:
        import yaml
        with open(cal_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return data.get('events', [])
    except Exception as e:
        logger.warning("人工事件日历加载失败: %s", e)
        return []


def get_upcoming_events(stock_code: str = "", days: int = 30) -> List[Dict]:
    """
    获取未来N天的事件日历

    Args:
        stock_code: 股票代码（空=全市场）
        days: 未来天数

    Returns:
        [{"date", "type", "code", "detail", "warning_level"}, ...]
    """
    events = []
    today = datetime.now().strftime("%Y-%m-%d")
    end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    # 1. 解禁事件（注：stock_restricted_release_summary_em 返回已发生数据）
    # 这里用作"近30天已发生解禁"参考，未来预警依赖人工维护
    df = _load_unlock_summary()
    if df is not None and not df.empty:
        # 取最近30天已发生的解禁事件作为参考
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        for _, row in df.iterrows():
            unlock_date = str(row.get("解禁时间", ""))[:10]
            if start_date <= unlock_date <= today:
                unlock_value = float(row.get("实际解禁市值", 0) or 0)
                stock_count = int(row.get("当日解禁股票家数", 0) or 0)
                warning = "高" if unlock_value > 1e10 else ("中" if unlock_value > 1e9 else "低")
                events.append({
                    "date": unlock_date,
                    "type": "解禁(已发生)",
                    "code": "",
                    "detail": f"解禁市值{unlock_value/1e8:.1f}亿, {stock_count}只股票",
                    "warning_level": warning,
                })

    # 2. 人工维护事件（财报节点等）
    manual = _load_manual_calendar() or []
    for ev in manual:
        ev_date = ev.get("date", "")
        if today <= ev_date <= end_date:
            if stock_code and ev.get("code", "") and ev["code"] != stock_code:
                continue
            events.append(ev)

    # 按日期排序
    events.sort(key=lambda x: x["date"])
    return events


def check_event_warning(stock_code: str) -> Dict[str, Any]:
    """
    检查个股的事件预警

    Returns:
        {
            "warning_level": "高/中/低/无",
            "events": [...],
            "detail": str,
        }
    """
    events = get_upcoming_events(stock_code, days=15)  # 未来15天
    if not events:
        return {"warning_level": "无", "events": [], "detail": "无事件预警"}

    # 取最高预警级别
    level_rank = {"高": 3, "中": 2, "低": 1, "无": 0}
    max_level = "无"
    for ev in events:
        ev_level = ev.get("warning_level", "低")
        if level_rank.get(ev_level, 0) > level_rank.get(max_level, 0):
            max_level = ev_level

    detail = "; ".join(f"{ev['date']} {ev['type']}({ev.get('warning_level','低')})" for ev in events)
    return {
        "warning_level": max_level,
        "events": events,
        "detail": detail,
    }


def get_market_event_summary() -> Dict[str, Any]:
    """全市场事件日历摘要（用于盘前推送）"""
    events = get_upcoming_events("", days=15)
    if not events:
        return {"count": 0, "high_warning": 0, "detail": "未来15天无事件"}

    high = sum(1 for ev in events if ev.get("warning_level") == "高")
    medium = sum(1 for ev in events if ev.get("warning_level") == "中")
    return {
        "count": len(events),
        "high_warning": high,
        "medium_warning": medium,
        "detail": f"未来15天{len(events)}个事件(高{high}/中{medium})",
        "events": events[:5],  # 只展示前5个
    }
