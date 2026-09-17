"""
【分型引擎】③量能改造 — 单元测试

问题（2026-09-14 改造前的实证）：
  1. 内外盘只标"(展示)"不判定——飞荣达 9/11 跌4.2%+外盘占优+超卖的见底
     信号被埋没在陈列里；
  2. "缩量"一词承载不同市场状态——量比0.89配6.5%换手与配1%换手被混为一谈；
  3. ③量能与④资金互不引用——外盘占优但主力净流出（散单接盘）无人识别。

验收样例：飞荣达 300602（方案 4.2）——抛压衰竭型 + 量价资金背离降一级。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzers.volume_pattern import (
    build_volume_pattern,
    classify,
    commission_cross_check,
    detect_conflict,
    drive_label,
    kline_position_inputs,
    limit_exempt,
    main_force_net3,
    pattern_summary_line,
    position_gear,
    render_volume_pattern,
    star_rating,
    turnover_gear,
    volume_gear,
)


def _feirongda_data(**overrides):
    """飞荣达 300602 验收样例数据（2026-09-11 收盘口径）。"""
    base = {
        "stock_name": "飞荣达", "stock_code": "300602",
        "current_price": 25.31, "change_pct": -4.2,
        "volume_ratio": 0.89, "turnover_rate": 6.55,
        "ma5": 26.8, "ma10": 27.5, "ma20": 27.9,
        "prior_high": 28.0, "recent_high": 28.0,
        "gain_20d": -0.15, "ma20_falling": True,
        "rsi6": 21,
        "tech_signals": {
            "order_flow": {
                "available": True, "outer_volume": 1347, "inner_volume": 1282,
                "imbalance_pct": 2.5,
            },
        },
        "institutional_holding": {
            "vote_score": -1, "vote_label": "偏空",
            "votes": {"main_force": {"raw": {"fund_flow_5d": [
                {"date": "2026-09-09", "value": -30_000_000},
                {"date": "2026-09-10", "value": -40_000_000},
                {"date": "2026-09-11", "value": -40_000_000},
            ]}}},
            "effective_vote_weights": {"main_force": 0.5},
        },
    }
    base.update(overrides)
    return base


def _flow(drive_pct=2.5):
    return {"available": True, "outer_volume": 1000, "inner_volume": 900,
            "imbalance_pct": drive_pct}


# ============================================================
# 1. 四个计算字段
# ============================================================

class TestGears:
    def test_volume_gear_bands(self):
        assert volume_gear(0.5) == "缩量"
        assert volume_gear(0.89) == "缩量"   # 飞荣达验收：0.89 标缩量
        assert volume_gear(0.9) == "平量"
        assert volume_gear(1.2) == "平量"
        assert volume_gear(2.5) == "温和放量"
        assert volume_gear(3.0) == "明显放量"
        assert volume_gear(3.5) == "明显放量"
        assert volume_gear(4.0) == "剧烈放量"

    def test_volume_gear_missing(self):
        assert volume_gear(None) is None
        assert volume_gear("") is None
        assert volume_gear(float("nan")) is None

    def test_turnover_gear_bands(self):
        assert turnover_gear(0.5) == "清淡"
        assert turnover_gear(1.5) == "正常"
        assert turnover_gear(6.55) == "活跃"   # 飞荣达：6.55% → 活跃
        assert turnover_gear(10.0) == "过热"
        assert turnover_gear(16.0) == "决战"
        assert turnover_gear(None) is None


class TestDrive:
    def test_active_drive_from_imbalance(self):
        from src.analyzers.volume_pattern import active_drive
        assert active_drive(_flow(2.5)) == 0.025
        assert active_drive({"available": False}) is None
        assert active_drive(None) is None

    def test_drive_label_sign_based(self):
        # 与分型矩阵同符号口径：+2.5% 即外盘占优（验收样例）
        assert drive_label(0.025) == "外盘占优"
        assert drive_label(-0.025) == "内盘占优"
        assert drive_label(0.0) == "均衡"
        assert drive_label(None) == "数据未取到"


class TestPosition:
    def test_high_requires_near_high_and_big_gain(self):
        # 贴前高 ≤10% 且 近20日涨幅 >30% → 高位
        assert position_gear(30.0, 29, 28, 27, 0.05, 0.35, False) == "高位"

    def test_near_high_without_gain_is_not_high(self):
        # P0-1：贴前高5% + 涨幅10% + MA20偏离<5% + 缺口>3% → 三子指标均弱 → 中继
        assert position_gear(28.2, 28.1, 27.9, 27.6, 0.05, 0.10, False) == "中继"

    def test_position_gear_ma20_dev_and_gap(self):
        # P0-1：MA20偏离>5% 或 距前高≤3% → 高位（无需20日涨幅>30%）
        assert position_gear(30.0, 29, 28, 27, 0.05, 0.10, False) == "高位"
        assert position_gear(27.9, 28, 28.1, 28.2, 0.02, 0.10, False) == "高位"

    def test_near_high_without_gain_and_flat_ma_is_not_low(self):
        # P0-1：贴前高但20日涨幅不足、MA非多头 → 至少中继，禁止落低位兜底桶
        assert position_gear(100.0, 95, 96, 97, 0.04, 0.10, False) == '中继'

    def test_near_high_breakdown_still_counts(self):
        # P0-1：贴前高但收盘<MA20 且 MA20 向下 → 破位优先于中继
        assert position_gear(90.0, 95, 96, 97, 0.04, 0.10, True) == '破位'

    def test_continuation_is_bullish_alignment(self):
        assert position_gear(30.0, 29, 28, 27, None, None, None) == "中继"

    def test_breakdown_needs_close_below_ma20(self):
        assert position_gear(25.31, 26.8, 27.5, 27.9, None, None, True) == "破位"

    def test_breakdown_without_slope_still_counts(self):
        # MA20 斜率缺失时按收盘<MA20 判破位（诚实降级，不静默跳过）
        assert position_gear(25.31, 26.8, 27.5, 27.9, None, None, None) == "破位"

    def test_close_below_ma20_with_rising_ma20_is_low(self):
        assert position_gear(25.31, 26.8, 27.5, 27.9, None, None, False) == "低位"

    def test_default_low(self):
        assert position_gear(10.0, 11, 12, 13, None, None, False) == "低位"


class TestKlineInputs:
    def test_gain_20d_and_ma20_slope(self):
        closes = [10.0] * 25 + [11.5] * 5   # 20日涨 15%，近段抬升
        out = kline_position_inputs([{"收盘": c} for c in closes])
        assert out["gain_20d"] == 11.5 / 10.0 - 1
        assert out["ma20_falling"] is False

    def test_falling_ma20(self):
        closes = [12.0] * 25 + [10.0] * 5
        out = kline_position_inputs([{"close": c} for c in closes])
        assert out["gain_20d"] == 10.0 / 12.0 - 1
        assert out["ma20_falling"] is True

    def test_insufficient_kline(self):
        assert kline_position_inputs([{"收盘": 10.0}]) == {
            "gain_20d": None, "ma20_falling": None, "high_52w": None,
        }
        assert kline_position_inputs(None) == {
            "gain_20d": None, "ma20_falling": None, "high_52w": None,
        }

    def test_kline_inputs_high_52w(self):
        # P0-1：近250日最高价（数据不足时用可得窗口）
        highs = [10.0 + i * 0.1 for i in range(30)]
        closes = [10.0 + i * 0.1 for i in range(30)]
        out = kline_position_inputs(
            [{"收盘": c, "最高": h} for c, h in zip(closes, highs)]
        )
        assert out["high_52w"] == max(highs[-250:])


# ============================================================
# 2. 八型分型矩阵
# ============================================================

class TestMatrix:
    def test_selling_exhaustion(self):
        # 跌 + D>0 + 缩量/平量（任意位置）→ 抛压衰竭型（飞荣达型）
        assert classify(-4.2, 0.025, "缩量", "破位") == "抛压衰竭型"
        assert classify(-2.0, 0.01, "平量", "低位") == "抛压衰竭型"

    def test_down_with_positive_drive_is_divergence(self):
        # 跌 + D>0 + 放量 → 外盘/价格方向矛盾，不能单边叫资金接货或出逃
        assert classify(-3.0, 0.08, "温和放量", "低位") == "放量下跌分歧型"
        assert classify(-5.0, 0.10, "明显放量", "中继") == "放量下跌分歧型"

    def test_wash_trade_trap(self):
        # 跌 + D>0 + 剧烈放量 + 高位 → 对倒诱多嫌疑
        assert classify(-6.0, 0.15, "剧烈放量", "高位") == "对倒诱多嫌疑"

    def test_real_selling_pressure(self):
        # 跌 + D<0 + 放量系（任意位置）→ 真实抛压型
        assert classify(-7.0, -0.20, "温和放量", "低位") == "真实抛压型"
        assert classify(-8.0, -0.30, "剧烈放量", "中继") == "真实抛压型"

    def test_grinding_decline(self):
        # 跌 + D<0 + 缩量/平量 → 阴跌不止型
        assert classify(-1.5, -0.06, "缩量", "破位") == "阴跌不止型"
        assert classify(-1.0, -0.02, "平量", "低位") == "阴跌不止型"

    def test_bearish_breakdown_with_positive_drive_stays_divergence(self):
        # 破位不能把外盘占优反向解释成“主动性出逃”；等资金/均价裁决
        assert classify(-6.0, 0.12, "明显放量", "破位") == "放量下跌分歧型"

    def test_carried_rally(self):
        # 涨/平 + D<0 + 主力净流转正 → 承接型上涨（P1-10 必要条件）
        assert classify(3.0, -0.10, '温和放量', '中继', main_force_net=0.2) == '承接型上涨'
        assert classify(0.0, -0.05, '平量', '低位', main_force_net=0.1) == '承接型上涨'

    def test_carried_rally_without_main_force_is_stall(self):
        # P1-10：涨/平 + D<0 但无主力承接证据 → 降为缩量滞涨观察
        assert classify(3.0, -0.10, '温和放量', '中继') == '缩量滞涨观察'
        assert classify(0.46, -0.019, '缩量', '中继', main_force_net=0.0) == '缩量滞涨观察'
        assert classify(0.46, -0.019, '缩量', '中继', main_force_net=-0.5) == '缩量滞涨观察'


    def test_distribution_at_high(self):
        # 涨 + D>=0 + 剧烈放量 + 高位 → 高位滞涨派发
        assert classify(2.0, 0.05, "剧烈放量", "高位") == "高位滞涨派发"
        assert classify(0.5, 0.0, "剧烈放量", "高位") == "高位滞涨派发"

    def test_healthy_advance(self):
        assert classify(3.0, 0.08, "温和放量", "中继") == "健康推进型"
        assert classify(1.0, 0.02, "缩量", "低位") == "健康推进型"

    def test_no_drive_no_pattern(self):
        # 内外盘缺失不做分型（不猜）——但量能档/位置档照常降级展示
        assert classify(-4.2, None, "缩量", "破位") is None
        data = _feirongda_data(tech_signals={"order_flow": {"available": False}})
        result = build_volume_pattern(data)
        assert result["pattern"] is None
        assert result["summary_short"] == "盘口数据不足"


# ============================================================
# 3. 冲突检测 + 委比交叉验证 + 豁免
# ============================================================

class TestConflicts:
    def test_feirongda_divergence_rule1(self):
        # 规则一：分型偏多 + 资金投票偏空 + 主力3日净流出 → 量价资金背离
        result = build_volume_pattern(_feirongda_data())
        assert result["pattern"] == "抛压衰竭型"
        conflict = result["conflict"]
        assert conflict and "量价资金背离" in conflict
        assert "主力3日净流出1.10亿" in conflict
        assert "权重0.5" in conflict
        assert "降一级" in conflict

    def test_rule1_triggers_on_vote_only(self):
        inst = {"vote_score": -1, "votes": {}, "effective_vote_weights": {}}
        text = detect_conflict("抛压衰竭型", inst)
        assert text and "资金投票-1票" in text

    def test_rule1_triggers_on_flow_only(self):
        inst = {"vote_score": 0, "votes": {"main_force": {"raw": {
            "fund_flow_5d": [{"date": "d1", "value": -5_000_000}]}}}}
        text = detect_conflict("健康推进型", inst)
        assert text and "主力3日净流出500万" in text

    def test_no_conflict_when_aligned(self):
        inst = {"vote_score": 2, "votes": {"main_force": {"raw": {
            "fund_flow_5d": [{"date": "d1", "value": 5_000_000}]}}}}
        assert detect_conflict("抛压衰竭型", inst) is None

    def test_rule2_capital_against_trend(self):
        # 规则二：分型偏空 + 资金投票偏多 → 资金逆势
        inst = {"vote_score": 2, "votes": {}}
        text = detect_conflict("真实抛压型", inst)
        assert text and "资金逆势" in text and "次日验证优先" in text
        # 中性票不触发
        assert detect_conflict("真实抛压型", {"vote_score": 0, "votes": {}}) is None
        # 承接型上涨（bias=0）不参与冲突检测
        assert detect_conflict("承接型上涨", {"vote_score": 2, "votes": {}}) is None

    def test_conflict_marks_summary_line(self):
        result = build_volume_pattern(_feirongda_data())
        assert result["summary_short"] == "⚠抛压衰竭·降一级"

    def test_main_force_net3_window(self):
        inst = {"votes": {"main_force": {"raw": {"fund_flow_5d": [
            {"date": "d1", "value": 100.0}, {"date": "d2", "value": 200.0},
            {"date": "d3", "value": -400.0}, {"date": "d4", "value": 50.0},
        ]}}}}
        # 只取最近3日（d2/d3/d4）
        assert main_force_net3(inst) == -150.0


class TestCommissionCheck:
    def test_same_direction(self):
        assert commission_cross_check(0.10, 25.0) == "委托一致(委比与主动差同向)"
        assert commission_cross_check(-0.10, -25.0) == "委托一致(委比与主动差同向)"

    def test_prop_suspicion(self):
        # 内盘占优 + 委比为正 → 托价嫌疑
        assert "托价嫌疑" in commission_cross_check(-0.10, 20.0)

    def test_press_absorb_suspicion(self):
        # 外盘占优 + 委比为负 → 压单吸筹或边拉边撤
        assert "压单吸筹" in commission_cross_check(0.10, -20.0)

    def test_noise_band_skipped(self):
        # |委比|<10% 挂单噪音不判同向/反向
        assert commission_cross_check(0.10, 5.0) is None
        assert commission_cross_check(0.10, None) is None
        assert commission_cross_check(None, 25.0) is None


class TestExemption:
    def test_limit_up_down_exempt_main_board(self):
        assert limit_exempt(10.02, "600000") and "豁免分型" in limit_exempt(10.02, "600000")
        assert limit_exempt(-9.9, "600000") and "封板内外盘失真" in limit_exempt(-9.9, "600000")
        assert limit_exempt(5.0, "600000") is None

    def test_exempt_pattern_skips_classification(self):
        # 飞荣达是创业板(20cm)，10% 不豁免；换主板票验豁免路径
        assert limit_exempt(10.05, "300602") is None
        data = _feirongda_data(stock_code="600000", change_pct=10.05)
        result = build_volume_pattern(data)
        assert result["exempt"] is True
        assert result["pattern"] is None
        lines = render_volume_pattern(result)
        assert "豁免" in lines[0]

    def test_20cm_board_not_false_exempted_mid_range(self):
        # 创业板/科创板 20% 限价：+15% 未封板不得误豁免
        assert limit_exempt(15.0, "300602") is None
        assert limit_exempt(15.0, "688627") is None
        assert limit_exempt(19.9, "300602") and "20%限价板" in limit_exempt(19.9, "300602")

    def test_st_board_uses_5pct_limit(self):
        assert limit_exempt(4.9, "600000", "ST某某") and "5%限价板" in limit_exempt(4.9, "600000", "ST某某")
        assert limit_exempt(3.0, "600000", "ST某某") is None


# ============================================================
# 4. 星级 + 渲染 + 一览行
# ============================================================

class TestStarsAndRender:
    def test_star_ratings(self):
        # 抛压衰竭：活跃换手 → ★★★（飞荣达 6.55%）
        assert star_rating("抛压衰竭型", "活跃", 0.025, "缩量") == 3
        assert star_rating("抛压衰竭型", "清淡", 0.025, "缩量") == 2
        assert star_rating("健康推进型", "活跃", 0.05, "温和放量") == 3
        assert star_rating("健康推进型", "正常", 0.05, "缩量") == 2
        assert star_rating("真实抛压型", None, None, None) == 3
        assert star_rating("阴跌不止型", None, None, None) == 2

    def test_overbought_caps_star_and_kills_resonance(self):
        # P0-2：RSI6>72 时星级封顶 + 健康推进型禁用方向与力度共振判读句
        data = _feirongda_data(
            change_pct=3.0, current_price=30.0,
            ma5=29, ma10=28, ma20=27, prior_high=30.5, recent_high=30.5,
            gain_20d=0.35, rsi6=75,
            tech_signals={'order_flow': {
                'available': True, 'outer_volume': 1000, 'inner_volume': 900,
                'imbalance_pct': 8.0,
            }},
            institutional_holding={'vote_score': 1, 'votes': {}},
        )
        result = build_volume_pattern(data)
        assert result['pattern'] == '健康推进型'
        assert result['star'] == 1
        assert '短线过热' in result['summary_short']
        assert '方向与力度共振' not in result['verdict']
        assert '回踩确认优先' in result['verdict']
        rendered = ' '.join(render_volume_pattern(result))
        assert '★☆☆' in rendered
        assert 'RSI6=75超买' in rendered

    def test_sample_time_formatted_readable(self):
        # P2-13：14位时间戳格式化为 MM-DD HH:MM，不得连排进条件句/数据行
        data = _feirongda_data(
            execution_plan={'volume_snapshot': {
                'volume_ratio': 0.89, 'turnover_rate': 6.55,
                'volume_ratio_sample_time': '20260917140219',
                'volume_ratio_caliber': '实时接口',
            }},
        )
        lines = render_volume_pattern(build_volume_pattern(data))
        assert '接口量比0.89@09-17 14:02' in lines[1]
        assert any('接口量比@09-17 14:02 回升1.2+' in line for line in lines)
        assert all('20260917140219' not in line for line in lines)

    def test_early_window_same_period_time_formatted(self):
        # P2-13：早盘同期量比时间戳同样格式化
        data = _feirongda_data(
            execution_plan={'volume_snapshot': {
                'volume_ratio': 3.0, 'turnover_rate': 6.55, 'is_early_window': True,
                'same_period_volume_ratio': 1.5,
                'same_period_sample_time': '20260917094200',
                'same_period_caliber': '同期累计',
            }},
        )
        lines = render_volume_pattern(build_volume_pattern(data))
        assert '同期量比1.50@09-17 09:42' in lines[1]

    def test_feirongda_acceptance_render(self):
        """方案 4.2 验收样例逐字段。"""
        lines = render_volume_pattern(build_volume_pattern(_feirongda_data()))
        assert lines[0] == "③量能 [抛压衰竭型] ★★★"
        assert lines[1] == (
            "③·数据: 接口量比0.89@时间未标注(缩量;口径未标注) | 换手6.55%(活跃) "
            "| 主动差+2.5%(外盘占优) | 位置:破位(距前高-9.6%｜MA20偏离-9.3%｜20日涨幅-15.0%)"
        )
        assert "RSI6=21超卖" in lines[2] and "跌不动了" in lines[2]
        assert any("量价资金背离" in line for line in lines)
        assert any("接口量比@时间未标注 回升1.2+" in line and "低吸复核" in line for line in lines)

    def test_render_without_order_flow(self):
        data = _feirongda_data(tech_signals={})
        lines = render_volume_pattern(build_volume_pattern(data))
        assert lines[0].startswith("③量能 [盘口数据不足]")
        assert "内外盘:N/A" in lines[0]

    def test_render_preserves_p90_hot_flag(self):
        data = _feirongda_data(
            execution_plan={"volume_snapshot": {
                "volume_ratio": 2.8, "turnover_rate": 12.0, "turnover_hot": True,
            }},
        )
        result = build_volume_pattern(data)
        assert result["turnover_hot"] is True
        rendered = " ".join(render_volume_pattern(result))
        assert ">P90过热" in rendered

    def test_summary_line(self):
        assert pattern_summary_line(_feirongda_data()) == "飞荣达[⚠抛压衰竭·降一级]"
        # 数据不足给出一览占位而非假结论
        assert pattern_summary_line({"stock_name": "X"}) == "X[盘口数据不足]"
        assert pattern_summary_line({}) == ""
        assert pattern_summary_line({"stock_name": "", "change_pct": -4.0}) == ""

    def test_summary_line_no_conflict(self):
        data = _feirongda_data(institutional_holding={"vote_score": 0, "votes": {}})
        result = build_volume_pattern(data)
        assert result["conflict"] is None
        assert pattern_summary_line(data) == "飞荣达[抛压衰竭·低吸观察]"


# ============================================================
# 5. 模版集成：观察卡与买入卡 ③ 行替换
# ============================================================

class TestTemplateIntegration:
    def test_observation_card_renders_pattern_block(self):
        from src.push.templates import _render_compact_observation_signal
        title, content = _render_compact_observation_signal(_feirongda_data())
        assert "③量能 [抛压衰竭型] ★★★" in content
        assert "③·判读" in content
        assert "量价资金背离" in content
        assert "③·确认" in content
        # 旧陈列文案不再出现
        assert "(展示)" not in content

    def test_observation_card_degrades_without_flow(self):
        from src.push.templates import _render_compact_observation_signal
        data = _feirongda_data(tech_signals={})
        _, content = _render_compact_observation_signal(data)
        assert "盘口数据不足" in content
        assert "④资金" in content

    def test_entry_card_lines_include_pattern_block(self):
        from src.push.templates import _entry_decision_lines
        data = _feirongda_data(
            execution_plan={"volume_snapshot": {
                "volume_ratio": 0.89, "turnover_rate": 6.55,
            }},
        )
        lines = _entry_decision_lines(data)
        joined = "\n".join(lines)
        assert "③量能 [抛压衰竭型] ★★★" in joined
        assert "③·数据" in joined
        assert "①方向" in joined and "④资金" in joined

    def test_summary_rows_for_report(self):
        # 一览行：分歧票带⚠、缩量阴跌票等待（方案 4.3 精智达型）
        jingzhida = {
            "stock_name": "精智达", "stock_code": "688627",
            "current_price": 40.0, "change_pct": -1.2,
            "volume_ratio": 0.6, "turnover_rate": 1.5,
            "ma5": 41.0, "ma10": 41.5, "ma20": 41.0,
            "tech_signals": {"order_flow": {
                "available": True, "outer_volume": 500, "inner_volume": 700,
                "imbalance_pct": -16.7,
            }},
        "institutional_holding": {"vote_score": 0, "votes": {}},
        }
        # P1-9：主动差-16.7%(≤-8%) 的真实样本归真实抛压，不再仅按量能档贴阴跌
        assert pattern_summary_line(jingzhida) == "精智达[真实抛压·风控]"

        # 阴跌窄域（量比≤1.0 且 跌幅≤1.5% 且 主动差>-8%）仍输出缩量阴跌等待
        grinding = {
            "stock_name": "精智达", "stock_code": "688627",
            "current_price": 40.0, "change_pct": -1.2,
            "volume_ratio": 0.6, "turnover_rate": 1.5,
            "ma5": 41.0, "ma10": 41.5, "ma20": 41.0,
            "tech_signals": {"order_flow": {
                "available": True, "outer_volume": 500, "inner_volume": 553,
                "imbalance_pct": -5.0,
            }},
            "institutional_holding": {"vote_score": 0, "votes": {}},
        }
        assert pattern_summary_line(grinding) == "精智达[缩量阴跌·等待]"


# ============================================================
# 6. 委比数据链：腾讯五档 → order_flow.wb_ratio
# ============================================================

class TestWbRatioWiring:
    @staticmethod
    def _kline():
        return [{
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
            "volume": 1000, "turnover_rate": 1.0,
        } for _ in range(30)]

    def test_wb_ratio_passthrough(self):
        from src.data_layer.stock_data import calc_tech_indicators
        result = calc_tech_indicators(
            self._kline(),
            realtime_quote={"outer_volume": 1347, "inner_volume": 1282,
                            "wb_ratio": 18.42},
        )
        assert result["order_flow"]["wb_ratio"] == 18.42

    def test_wb_ratio_absent_is_none(self):
        from src.data_layer.stock_data import calc_tech_indicators
        result = calc_tech_indicators(
            self._kline(),
            realtime_quote={"outer_volume": 1347, "inner_volume": 1282},
        )
        assert result["order_flow"]["wb_ratio"] is None

    def test_commission_note_reaches_render(self):
        data = _feirongda_data()
        data["tech_signals"]["order_flow"]["wb_ratio"] = 30.0
        rendered = " ".join(render_volume_pattern(build_volume_pattern(data)))
        assert "委托一致" in rendered
