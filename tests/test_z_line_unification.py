"""
【Phase4 P0-2】Z 线统一（执行止损 = 假说结构位）— 回归测试

依据：博杰 9/7 止损线 85.54 与当日跌停价 85.53 吻合到分——
用制度边界充当结构破位判定，直接污染三层输出：
RRR 0.59（本应 3.0）、置信度降档、仓位缩减。
验证：RRR 应从 0.59 升至约 3.0，置信度从 3/6 回到 4~5/6。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest


def _bojie_tech(**overrides):
    """博杰 9/7 报告数据：现价104.53 / MA25=99.39 / 昨收95.03 / MA10=102.77"""
    tech = {
        "current_price": 104.53,
        "ma25": 99.39,          # 突破位（结构位）
        "ma10": 102.77,         # Y 基准（主档）
        "ma5": 100.17,
        "ma20": 96.5,
        "recent_high": 108.0,
        "tech_signals": {"vote_score": 2.0},   # 技术同向+1
        "institutional_holding": {"vote_score": 0, "votes": {}},
        "market_score": 5.0,
    }
    tech.update(overrides)
    return tech


class TestBojieZLineUnification:
    """博杰 9/7 信号重算（决策记录验证项）"""

    def test_old_atr_buffer_mode_reproduces_85_54(self):
        """回退族复现病灶：结构位99.39 − 1.5×ATR(9.23) = 85.54 ≈ 当日跌停价85.53"""
        from src.analyzers.hypothesis import calculate_paired_stop
        z, ref, _ = calculate_paired_stop(
            "价量突破", _bojie_tech(), benchmark_price=102.77, atr=9.23,
            config={"hypothesis_gate": {"z_line_mode": "atr_buffer"}},
        )
        assert ref == pytest.approx(99.39, abs=0.01)
        assert z == pytest.approx(85.54, abs=0.02)

    def test_bare_structure_z_equals_structure_level(self):
        """裸结构位对照族（显式配置）：Z = 99.39（跌破即逻辑死亡）；
        默认 buffered_structure：Z = 91.44（缓冲被 8% 上限压住，
        不再拖到跌停价 85.54——Phase5 回炉：决策记录“结构位或结构位
        加 1~2 倍 ATR 缓冲”的后半句）"""
        from src.analyzers.hypothesis import calculate_paired_stop
        z, ref, note = calculate_paired_stop(
            "价量突破", _bojie_tech(), benchmark_price=102.77, atr=9.23,
            config={"hypothesis_gate": {"z_line_mode": "bare_structure"}},
        )
        assert z == pytest.approx(99.39, abs=0.01)
        assert ref == pytest.approx(99.39, abs=0.01)
        assert "99.39" in note
        # 默认 buffered：ATR/ref=9.3% ≥ 5% → 高波动档 k=1.5 →
        # buffer=13.85 被 8% 上限（7.95）钳制 → Z=91.44
        z_buf, _, _ = calculate_paired_stop(
            "价量突破", _bojie_tech(), benchmark_price=102.77, atr=9.23,
        )
        assert z_buf == pytest.approx(91.44, abs=0.02)
        assert z_buf > 85.54 + 5.0            # 与跌停价拉开安全距离

    def test_bojie_rrr_recalc_from_059_to_30(self):
        """RRR 重算：(112.89−102.77)/(102.77−99.39) = 2.99 ≈ 3.0"""
        from src.analyzers.signal_plan import build_execution_plan
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=102.77,          # Y（MA10 主档）
            stop_loss=99.39,                 # Z = 结构位（P0-2 统一后）
            target_range=[112.89, 120.21],   # W（阻力位）
            tech_data=_bojie_tech(),
            sector_status="main_trend",
            hypothesis_x="突破MA25(昨收95.03在昨日MA25下方，今价104.53站上99.39)；量能突破60日均量",
        )
        assert plan.execute is True
        assert plan.hypothesis_rejected is False
        # 风险 = 102.77-99.39 = 3.38；回报低沿 = 112.89-102.77 = 10.12
        assert plan.rrr_low == pytest.approx(2.99, abs=0.05)

    def test_bojie_confidence_recovers_from_3_to_4_of_6(self):
        """置信度重算：RRR0.59<1.5 降档消失 + RRR≥2.5 质量票 → 4/6，不再被评分体系错杀"""
        from src.analyzers.signal_plan import build_execution_plan
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=102.77,
            stop_loss=99.39,
            target_range=[112.89, 120.21],
            tech_data=_bojie_tech(),
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
        )
        # 技术同向+1 / 机构+0 / 主线+1 / 无矛盾+1 = 3，RRR2.99≥2.5 → +1 = 4
        assert plan.confidence_score == 4
        assert any("RRR2.99" in d and "+1" in d for d in plan.confidence_details)
        assert not any("降档" in d for d in plan.confidence_details)
        assert plan.confidence in ("高", "中")

    def test_old_z_line_yields_rrr_059_baseline(self):
        """对照基线：旧 Z=85.54 时 RRR = 10.12/17.23 = 0.59（病灶复现）"""
        from src.analyzers.signal_plan import build_execution_plan
        plan = build_execution_plan(
            entry_type="价量突破",
            benchmark_price=102.77,
            stop_loss=85.54,                 # 旧 ATR 缓冲止损
            target_range=[112.89, 120.21],
            tech_data=_bojie_tech(),
            sector_status="main_trend",
            hypothesis_x="突破MA25；量能突破60日均量",
        )
        assert plan.rrr_low == pytest.approx(0.59, abs=0.02)
        assert any("RRR0.59" in d and "降档" in d for d in plan.confidence_details)

    def test_hypothesis_sentence_uses_structure_z(self):
        """出厂假说整句：默认 buffered 模式 Z 锚定结构位下方缓冲区
        （91.44，既不是跌停价 85.54 也不是裸结构位 99.39）"""
        from src.analyzers.hypothesis import build_entry_hypothesis
        hyp = build_entry_hypothesis(
            entry_type="价量突破",
            tech_data=_bojie_tech(),
            benchmark_price=102.77,
            target_range=[112.89, 120.21],
            trigger_reason="突破MA25；量能突破60日均量",
            atr=9.23,
        )
        assert hyp.falsifiable
        assert hyp.exit_z == pytest.approx(91.44, abs=0.02)
        assert hyp.z_reference == pytest.approx(99.39, abs=0.01)   # 锚仍是结构位
        sentence = hyp.sentence()
        assert "99.39" in sentence           # 结构位在认错描述里可见
        assert "85.54" not in sentence        # 不再拖到跌停价
        assert "91.44" in sentence           # 执行止损 = 结构位下方缓冲


class TestZModeMetadata:
    """作废条件的数据基础设施：假说携带 z_line_mode，双模式对照统计"""

    def test_z_mode_comparison_buckets_by_mode(self):
        from src.feedback.strategy_stats import z_mode_comparison
        trades = [
            {"strategy": "价量突破", "pnl_pct": -3.0, "hypothesis": {"z_line_mode": "bare_structure"}},
            {"strategy": "价量突破", "pnl_pct": 2.0, "hypothesis": {"z_line_mode": "bare_structure"}},
            {"strategy": "价量突破", "pnl_pct": 1.0, "hypothesis": {"z_line_mode": "atr_buffer"}},
        ]
        comparison = z_mode_comparison(trades)
        assert comparison["bare_structure"]["trades"] == 2
        assert comparison["bare_structure"]["expectancy_pct"] == -0.5
        assert comparison["atr_buffer"]["trades"] == 1
        assert "不足30笔" in comparison["bare_structure"]["note"]

    def test_z_mode_comparison_handles_json_string_hypothesis(self):
        import json
        from src.feedback.strategy_stats import z_mode_comparison
        trades = [
            {"strategy": "价量突破", "pnl_pct": 5.0,
             "hypothesis": json.dumps({"z_line_mode": "bare_structure"})},
        ]
        comparison = z_mode_comparison(trades)
        assert comparison["bare_structure"]["trades"] == 1
