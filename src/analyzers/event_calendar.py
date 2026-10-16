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
_scheduled_report_cache: Optional[Any] = None
_scheduled_report_cache_date: str = ""
_scheduled_report_period: str = ""


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


def _current_report_period(now: Optional[datetime] = None) -> str:
    """当前财报预约披露期；到达法定截止日后切换下一期。"""
    now = now or datetime.now()
    year = now.year
    if now.month < 4 or (now.month == 4 and now.day <= 30):
        return f"{year}一季"
    if now.month < 8 or (now.month == 8 and now.day <= 31):
        return f"{year}半年报"
    if now.month < 10 or (now.month == 10 and now.day <= 31):
        return f"{year}三季"
    return f"{year}年报"


def _scheduled_date(row: Dict) -> str:
    """优先使用变更后预约日；未变更回退首次预约。"""
    for key in ("三次变更", "二次变更", "初次变更", "首次预约"):
        value = row.get(key)
        if value is None:
            continue
        try:
            import pandas as pd
            if pd.isna(value):
                continue
        except (ImportError, TypeError, ValueError):
            pass
        text = str(value).strip()
        if text and text.lower() not in ("nan", "nat", "none"):
            return str(value)[:10]
    return ""


def _load_scheduled_reports(now: Optional[datetime] = None):
    """加载当前期全市场预约披露表（当日缓存）。"""
    global _scheduled_report_cache, _scheduled_report_cache_date, _scheduled_report_period
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    period = _current_report_period(now)
    if _scheduled_report_cache_date != today or _scheduled_report_period != period:
        _scheduled_report_cache = None
        _scheduled_report_cache_date = today
        _scheduled_report_period = period
    if _scheduled_report_cache is None:
        try:
            import akshare as ak
            from ..data_layer.akshare_safe import call_ak_with_timeout
            _scheduled_report_cache = call_ak_with_timeout(
                ak.stock_report_disclosure,
                market="沪深京",
                period=period,
                timeout=30,
            )
            if _scheduled_report_cache is not None:
                logger.info(
                    "财报预约披露加载: %s %d行", period, len(_scheduled_report_cache)
                )
        except Exception as e:
            logger.warning("财报预约披露加载失败: %s", e)
            _scheduled_report_cache = None
    return _scheduled_report_cache, period


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

    # 3. 当前财报期预约披露：自动覆盖人工表遗漏的个股三季报/年报等节点。
    report_df, report_period = _load_scheduled_reports()
    if report_df is not None and not report_df.empty:
        try:
            from ..config_models import load_config
            portfolio = load_config("portfolio.yaml") or {}
            tracked_codes = {
                str(item.get("code") or "")
                for item in (portfolio.get("stocks") or [])
                if item.get("code")
            }
            if stock_code:
                tracked_codes &= {str(stock_code)}
            seen_reports = {
                (str(ev.get("code") or ""), str(ev.get("date") or ""), "财报")
                for ev in events
            }
            for _, row in report_df.iterrows():
                code = str(row.get("股票代码") or "").zfill(6)
                if code not in tracked_codes:
                    continue
                report_date = _scheduled_date(row)
                if not report_date or not (today <= report_date <= end_date):
                    continue
                key = (code, report_date, "财报")
                if key in seen_reports:
                    continue
                seen_reports.add(key)
                name = str(row.get("股票简称") or code)
                events.append({
                    "date": report_date,
                    "type": "财报",
                    "code": code,
                    "detail": f"{name}{report_period}预约披露（前2日谨慎，防业绩雷）",
                    "warning_level": "中",
                })
        except Exception as e:
            logger.warning("财报预约披露解析失败: %s", e)

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
