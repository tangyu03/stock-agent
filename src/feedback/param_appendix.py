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
  - strategy_stats（RRR/止损模式的样本验证状态）
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

    # ── Z 线模式：裸结构位 vs ATR 缓冲 ──
    z_mode = "bare_structure"
    z_note = "执行止损=假说结构位（P0-2 博杰实证：结构位99.39，缓冲版拖至85.54=跌停价）"
    try:
        from ..config_models import load_config
        timing_cfg = load_config("timing.yaml") or {}
        gate = (timing_cfg or {}).get("hypothesis_gate") or {}
        if gate:
            z_mode = str(gate.get("z_line_mode") or "bare_structure")
            if z_mode == "atr_buffer":
                z_note = "结构位-1.5×ATR（回退族：30笔统计显示缓冲版期望更高时切换）"
    except Exception:
        pass
    z_stats = None
    if stats:
        z_stats = stats.get("Z线模式对照")
    z_verify = "样本不足（30笔起步，双模式对照见策略统计）"
    if z_stats and isinstance(z_stats.get("stats"), dict):
        z_verify = str(z_stats["stats"].get("note") or z_verify)
    lines.append(f"  Z线模式:{z_mode} | 出处:{z_note} | 验证:{z_verify}")

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

    # ── 防守追强：风险预算仓位 ──
    dc_cfg = (cfg.get("defensive_chase") or {})
    budget = float(dc_cfg.get("risk_budget_pct") or 0.01) * 100
    dc_verify = "30笔滚动统计（胜率<30%且期望<0 → 回退禁用，踏空成本留档）"
    if stats:
        dc = stats.get("确认追强@防守")
        if dc and isinstance(dc.get("stats"), dict) and dc["stats"].get("trades", 0) >= 30:
            dc_verify = (
                f"样本{dc['stats'].get('trades')}笔，胜率{dc['stats'].get('win_rate', 0) * 100:.1f}%，"
                f"期望{dc['stats'].get('expectancy_pct', 0):+.2f}%"
            )
    lines.append(
        f"  防守追强试探仓 | 出处:单笔风险预算{budget:.0f}%÷止损距离(封顶1/3) | 验证:{dc_verify}"
    )

    # ── 仓位层→账户层：预算绝对值显式披露 ──
    # 蘅东光 9/8 验收：300股×单股风险88.56=26,568 元单笔风险敞口，按
    # 1% 风险预算反推隐含 265 万账户——此前预算基数与账户口径从不披露。
    try:
        from ..config_models import load_config
        timing_cfg = (load_config("timing.yaml") or {}).get("timing") or {}
        pb = (timing_cfg or {}).get("position_budget") or {}
        budget_per_stock = float(pb.get("budget_per_stock") or 250000)
        account_value = float(pb.get("account_value") or 1000000)
        lines.append(
            f"  建议仓位预算 | 单股预算基数{budget_per_stock/10000:.0f}万 | "
            f"账户口径{account_value/10000:.0f}万(1%风险预算={account_value*budget/100:,.0f}元) | "
            f"出处:config/timing.yaml position_budget（绝对值显式化，反推不再黑箱）"
        )
    except Exception:
        pass

    lines.append("  （参数被回测推翻时更新；附录机制永不作废）")
    return "\n".join(lines)
