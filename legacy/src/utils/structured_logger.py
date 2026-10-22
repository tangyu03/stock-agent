"""
结构化日志工具（P0 新增）

提供 JSON 格式日志 + trace_id 注入，用于：
1. 故障排查时按 trace_id 串联一次完整调度链路
2. 信号命中率/准确率等运营指标的基础数据源
3. 与 ELK/Loki 等日志聚合系统兼容

使用方法：
    from .utils.structured_logger import get_structured_logger
    logger = get_structured_logger(__name__)
    logger.info("盘中检查开始", extra={
        "phase": "intraday",
        "market_mode": "defend",
        "stock_count": 18,
    })

设计原则：
1. 完全兼容标准 logging.Logger 接口（无需修改业务代码大量日志调用）
2. 默认输出 JSON 格式（一行一条），可降级为纯文本（开发环境）
3. trace_id 通过 contextvars 注入，单线程内自动传播
4. 不引入第三方依赖（仅标准库）
"""
from __future__ import annotations
import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, Optional

# trace_id 上下文变量（单线程内自动传播）
_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")


def set_trace_id(trace_id: Optional[str] = None) -> str:
    """设置当前上下文的 trace_id，返回设置后的值"""
    tid = trace_id or uuid.uuid4().hex[:16]
    _trace_id_ctx.set(tid)
    return tid


def get_trace_id() -> str:
    """获取当前上下文的 trace_id"""
    return _trace_id_ctx.get()


def clear_trace_id() -> None:
    """清除当前上下文的 trace_id"""
    _trace_id_ctx.set("")


class StructuredJsonFormatter(logging.Formatter):
    """JSON 行格式化器

    输出格式：
    {"ts":"2026-07-17T10:30:00","level":"INFO","logger":"mod.name",
     "trace_id":"abc123","msg":"盘中检查开始","phase":"intraday",...}
    """

    # 标准字段（不出现在 extra 中）
    _STD_KEYS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message",
    })

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": record.getMessage(),
        }

        # 附加 extra 字段
        for key, value in record.__dict__.items():
            if key not in self._STD_KEYS and not key.startswith("_"):
                try:
                    json.dumps(value, ensure_ascii=False)  # 测试可序列化
                    log_entry[key] = value
                except (TypeError, ValueError):
                    log_entry[key] = repr(value)

        # 异常信息
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


class PlainTextFormatter(logging.Formatter):
    """纯文本格式化器（开发环境友好）"""

    def format(self, record: logging.LogRecord) -> str:
        tid = get_trace_id()
        tid_str = f"[{tid}] " if tid else ""
        msg = record.getMessage()
        # 附加 extra 字段
        extra_parts = []
        for key, value in record.__dict__.items():
            if key not in StructuredJsonFormatter._STD_KEYS and not key.startswith("_"):
                extra_parts.append(f"{key}={value}")
        extra_str = " " + " ".join(extra_parts) if extra_parts else ""
        return (f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} "
                f"{tid_str}{record.levelname[:4]} {record.name}: {msg}{extra_str}")


def get_structured_logger(
    name: str,
    json_format: Optional[bool] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    获取结构化 logger

    Args:
        name: logger 名称（通常 __name__）
        json_format: True=JSON 格式, False=纯文本, None=自动判断（生产=JSON, 开发=文本）
        level: 日志级别

    Returns:
        logging.Logger 实例（已配置 handler，不传播到 root）
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # 已配置过
        return logger

    # 自动判断格式：环境变量 LOG_FORMAT=json 强制 JSON，否则纯文本
    if json_format is None:
        import os
        json_format = os.environ.get("LOG_FORMAT", "").lower() == "json"

    logger.setLevel(level)
    logger.propagate = True  # P0-9: 传播到 root logger，让 FileHandler 写入文件

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        StructuredJsonFormatter() if json_format else PlainTextFormatter()
    )
    logger.addHandler(handler)

    return logger


def configure_root_logger(json_format: Optional[bool] = None, level: int = logging.INFO) -> None:
    """
    配置 root logger（应用启动时调用一次）

    替代 logging.basicConfig，所有未配置 handler 的 logger 都会传播到这里。
    """
    import os
    if json_format is None:
        json_format = os.environ.get("LOG_FORMAT", "").lower() == "json"

    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler（避免重复）
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        StructuredJsonFormatter() if json_format else PlainTextFormatter()
    )
    root.addHandler(handler)


__all__ = [
    "set_trace_id", "get_trace_id", "clear_trace_id",
    "StructuredJsonFormatter", "PlainTextFormatter",
    "get_structured_logger", "configure_root_logger",
]
