"""
参数附录（决策记录 P3-3）— 每个参数的出处与验证状态
=====================================================

依据：
  RRR 1.5 隐含 40% 胜率假设七天未验证——参数没有验证状态就是
  "拍脑袋的数字"。附录让每份报告底部可审计：参数值、出处
  （隐含假设）、当前验证状态（样本数/胜率/期望）。

作废条件：参数本身被回测推翻时更新，附录机制永不作废。

数据源：
  - config/timing.yaml（参数值）
  - strategy_stats（RRR 的样本验证状态）
  - volume_projection 误差记录表（外推口径的验证状态）
  - 卖出分级OR 过敏感统计
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _fmt_pct(value, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "N/A"


def build_param_appendix(
    config: Optional[Dict] = None,
    stats: Optional[Dict] = None,
) -> str:
    """构建参数附录文本（收盘版/盘中版底部）。

    Args:
        config: timing 配置（timing.yaml 加载后的 dict）
        stats: 策略分层统计（evaluate_kill_switch 的返回值，可缺省）
    """
    cfg = config or {}
    lines = ["⚙ 参数附录（出处与验证状态——拒绝无出处的数字）:"]

    # ── RRR 1.5：隐含 40% 胜率假设 ──
    rrr_threshold = 1.5
    try:
        from ..config_models import load_config
        timing_cfg = load_config("timing.yaml") or {}
        rrr_threshold = float((timing_cfg or {}).get("rrr_min") or 1.5)
    except Exception:
        pass
    implied_win_rate = 1.0 / (1.0 + rrr_threshold) * 100
    sample_text = "样本不足（七天未验证）"
    if stats:
        total_trades = sum(
            (info.get("stats") or {}).get("trades", 0)
            for info in stats.values()
            if isinstance(info, dict)
        )
        if total_trades > 0:
            wins = sum((info.get("stats") or {}).get("wins", 0) for info in stats.values() if isinstance(info, dict))
            sample_text = f"闭合样本{total_trades}笔，实测胜率{wins / total_trades * 100:.1f}%"
    lines.append(
        f"  RRR门槛{rrr_threshold:.1f} | 出处:隐含胜率假设{implied_win_rate:.0f}% "
        f"(1/(1+{rrr_threshold:.1f})) | 验证:{sample_text}"
    )

    # ── 量能外推：U 型分位表 + 误差记录 ──
    proj_text = "记录表尚无样本（每日收盘比对 外推vs实际，误差进表）"
    try:
        from ..analyzers.volume_projection import projection_error_report
        report = projection_error_report()
        if report.get("samples", 0) > 0:
            proj_text = str(report.get("note") or "")
    except Exception:
        pass
    lines.append(
        "  量能外推 | 出处:午前累计量÷U型分位(10:00前/封板禁用) "
        f"| 验证:{proj_text}"
    )

    # ── 卖出分级 OR：过敏感统计 ──
    graded_text = "周触发计数从拦截当日开始积累（>5次且过半3日内回本 → 回调阈值而非回退AND）"
    try:
        from ..feedback.strategy_stats import graded_exit_sensitivity
        sensitivity = graded_exit_sensitivity()
        if sensitivity.get("week_triggers", 0) > 0:
            graded_text = (
                f"近7日止损类触发{sensitivity.get('week_triggers')}次"
                f"（{sensitivity.get('note', '')}）"
            )
    except Exception:
        pass
    lines.append(f"  卖出分级OR | 出处:止损类任一即出(P0-3) | 验证:{graded_text}")

    lines.append("  （参数被回测推翻时更新；附录机制永不作废）")
    return "\n".join(lines)
