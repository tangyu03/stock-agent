"""
Capital Flow Analysis - 资金流向分析

功能：
- 主力资金流向（问财 OpenAPI）
- 北向资金分析（AKShare）
- 龙虎榜数据（AKShare）

数据源：
- 个股主力资金: 同花顺问财 OpenAPI（替代东财爬虫，稳定无反爬）
- 北向资金: 东方财富（AKShare，暂无问财对应接口）
- 龙虎榜: 东方财富（AKShare）
"""

import akshare as ak
import pandas as pd
import sys
import os
from typing import Dict
from datetime import datetime

# 确保能引用项目内部的 iwencai_api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def analyze_main_force(code: str) -> Dict:
    """
    主力资金分析（问财 OpenAPI）

    从问财正规 API 查询个股主力资金流向，替代原有的
    ak.stock_individual_fund_flow（东方财富爬虫，频繁反爬超时）。

    Args:
        code: 6位股票代码，如 "300308"

    Returns:
        {
            "code": str,
            "updated": str,
            "main_force_net": float,   # 主力净流入（万元）
            "signal": "流入"|"流出",
            "source": "问财OpenAPI"
        }
    """
    try:
        from src.data_layer.iwencai_api import query_stock_fund_flow

        result = query_stock_fund_flow(code)
        if result and result.get("main_net") is not None:
            return {
                "code": code,
                "updated": datetime.now().isoformat(),
                "main_force_net": round(result["main_net"] / 10000, 2),  # 元 → 万元
                "super_large_net": round(result.get("super_large_net", 0) / 10000, 2),
                "large_net": round(result.get("large_net", 0) / 10000, 2),
                "medium_net": round(result.get("medium_net", 0) / 10000, 2),
                "small_net": round(result.get("small_net", 0) / 10000, 2),
                "signal": result["signal"],
                "source": "问财OpenAPI",
            }
        return {"error": "问财API返回空数据", "code": code}
    except Exception as e:
        return {"error": str(e), "code": code}


def analyze_northbound(code: str) -> Dict:
    """北向资金分析（保留 AKShare，暂无问财对应接口）"""
    try:
        # 获取北向资金持股数据
        northbound = ak.stock_hsgt_individual_em(symbol=code)

        if northbound.empty:
            return {"error": "无北向资金数据"}

        latest = northbound.iloc[-1]

        return {
            "code": code,
            "updated": datetime.now().isoformat(),
            "holding_shares": round(float(latest['持股数']) / 1000000, 2),
            "holding_pct": round(float(latest['持股比例']), 2),
            "change": round(float(latest['持股变动']), 2),
            "signal": "增持" if float(latest['持股变动']) > 0 else "减持"
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    print("测试资金流向分析")
    print("=" * 50)

    main_result = analyze_main_force("300308")
    print(f"主力资金：{main_result}")

    northbound_result = analyze_northbound("300308")
    print(f"北向资金：{northbound_result}")
