"""
数值工具函数
"""
from typing import Any, Optional


def safe_float(v: Any, default: float = 0.0) -> float:
    """
    安全转 float，失败返回 default

    支持：
    - 数字类型（int/float）
    - 数字字符串（"123.45"）
    - 带百分号的字符串（"12.5%"）
    - 带逗号的字符串（"1,234.56"）
    - None / 空字符串 → default
    """
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(',', '').replace('%', '')
        if not s or s in ('-', '--', 'null', 'None', 'nan', 'NaN'):
            return default
        try:
            return float(s)
        except (ValueError, TypeError):
            return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(v: Any, default: int = 0) -> int:
    """安全转 int"""
    f = safe_float(v, default)
    try:
        return int(f)
    except (ValueError, TypeError):
        return default
