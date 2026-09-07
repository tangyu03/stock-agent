"""
【二】主题归属修正层 — 回归测试（Phase2-B）

核心案例（用户实测批评）：
  澜起/海光/大普微/中科飞测/芯碁微装/胜蓝/兆易创新 被误归"电子化学品"，
  骄成超声被误归"电池"，创世纪被误归"自动化设备"——
  而板块状态（主线/退潮/轮动）直接决定"禁追强/低吸可用"闸门。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import src.analyzers.theme_attribution as ta
from src.analyzers.theme_attribution import (
    apply_theme_attribution,
    make_board_status_lookup,
    resolve_stock_theme,
    reset_theme_state,
)


@pytest.fixture(autouse=True)
def _fresh_map():
    reset_theme_state()
    yield
    reset_theme_state()


# ============================================================
# 单股归属解析
# ============================================================

class TestResolveStockTheme:

    def test_lanqi_remapped_from_electronic_chemicals_to_storage(self):
        """澜起科技：电子化学品 → 存储（用户指认的第一大错配）"""
        attr = resolve_stock_theme("688008", "澜起科技",
                                   fallback_sector="电子化学品",
                                   fallback_status="rotational")
        assert attr["remapped"] is True
        assert attr["matched_by"] == "override"
        assert attr["theme"] == "存储"
        assert attr["original_sector"] == "电子化学品"
        assert "电子化学品→存储" in attr["evidence"]

    def test_haiguang_remapped_to_compute_hardware(self):
        attr = resolve_stock_theme("688041", "海光信息",
                                   fallback_sector="电子化学品",
                                   fallback_status="main_trend")
        assert attr["theme"] == "算力硬件"

    def test_zhaoyi_innovation_storage(self):
        attr = resolve_stock_theme("603986", "兆易创新",
                                   fallback_sector="电子化学品",
                                   fallback_status="rotational")
        assert attr["theme"] == "存储"

    def test_midsemicon_equipment_group(self):
        """中科飞测/芯碁微装/金海通 → 半导体设备"""
        for code, name in [("688361", "中科飞测"), ("688630", "芯碁微装"),
                           ("603061", "金海通"), ("688392", "骄成超声")]:
            attr = resolve_stock_theme(code, name,
                                        fallback_sector="电子化学品",
                                        fallback_status="rotational")
            assert attr["theme"] == "半导体设备", f"{code} 应归属半导体设备"

    def test_jiaocheng_reshaped_from_battery(self):
        """骄成超声：电池 → 半导体设备（原状态跟电池轮动走是错配）"""
        attr = resolve_stock_theme("688392", "骄成超声",
                                   fallback_sector="电池",
                                   fallback_status="rotational")
        assert attr["original_sector"] == "电池"
        assert attr["theme"] == "半导体设备"

    def test_chuangshiji_reshaped_from_automation(self):
        """创世纪：自动化设备 → 3C设备(苹果链)"""
        attr = resolve_stock_theme("300083", "创世纪",
                                   fallback_sector="自动化设备",
                                   fallback_status="rotational")
        assert attr["theme"] == "3C设备"
        assert "3C设备" in attr["display"]

    def test_unmapped_stock_falls_back(self):
        """未映射股票：保持原行业链路结果（兜底行为不变）"""
        attr = resolve_stock_theme("600000", "浦发银行",
                                   fallback_sector="银行",
                                   fallback_status="main_trend")
        assert attr["remapped"] is False
        assert attr["matched_by"] == "fallback"
        assert attr["display"] == "银行"
        assert attr["status"] == "main_trend"


# ============================================================
# 状态判定：代理板块最严格
# ============================================================

class TestThemeStatus:

    def test_proxy_strictest_status_wins(self):
        """半导体设备代理 [半导体(main_trend), 专用设备(retreating)] → 退潮最严格"""
        board = {"半导体": "main_trend", "专用设备": "retreating"}
        attr = resolve_stock_theme("688361", "中科飞测",
                                   fallback_sector="电子化学品",
                                   fallback_status="main_trend",
                                   board_status_fn=board.get)
        assert attr["status"] == "retreating"
        assert attr["status_source"] == "theme_proxy"

    def test_proxy_unavailable_falls_back_to_original(self):
        attr = resolve_stock_theme("688008", "澜起科技",
                                   fallback_sector="电子化学品",
                                   fallback_status="rotational",
                                   board_status_fn=lambda name: None)
        assert attr["status"] == "rotational"
        assert attr["status_source"] == "fallback"

    def test_storage_follows_semiconductor_board(self):
        """澜起跟存储主题（代理=半导体）走，不再被"电子化学品"的退潮误杀"""
        board = {"半导体": "main_trend", "电子化学品": "retreating"}
        attr = resolve_stock_theme("688008", "澜起科技",
                                   fallback_sector="电子化学品",
                                   fallback_status="retreating",  # 原链路：电子化学品退潮
                                   board_status_fn=board.get)
        assert attr["status"] == "main_trend"  # 修正后：跟半导体主线


# ============================================================
# 批量应用 + 引擎接线
# ============================================================

class TestApplyThemeAttribution:

    def test_batch_remap_mutates_dicts(self):
        stock_sector = {"688008": "电子化学品", "688041": "电子化学品",
                        "600000": "银行"}
        stock_sector_status = {"688008": "rotational", "688041": "rotational",
                               "600000": "main_trend"}
        board = {"半导体": "main_trend", "计算机设备": "rotational",
                 "通信设备": "main_trend"}
        report = apply_theme_attribution(
            stock_sector, stock_sector_status,
            {"688008": "澜起科技", "688041": "海光信息", "600000": "浦发银行"},
            board.get,
        )
        assert report["hit"] == 2
        assert stock_sector["688008"] == "存储"
        assert stock_sector_status["688008"] == "main_trend"  # 半导体主线
        assert stock_sector["688041"] == "算力硬件"
        assert stock_sector["600000"] == "银行"  # 兜底不动
        assert stock_sector_status["600000"] == "main_trend"

    def test_board_status_lookup_from_sector_map(self):
        lookup = make_board_status_lookup({"半导体": "retreating"}, use_ranking=False)
        assert lookup("半导体") == "retreating"
        assert lookup("不存在的板块") is None

    def test_lookup_matching_no_ranking_no_kline(self):
        """无排名无 K 线兜底时：查不到 → None（不发明状态）"""
        lookup = make_board_status_lookup({}, use_ranking=False, use_kline=False)
        assert lookup("半导体") is None

    def test_engine_sector_gate_uses_theme_status(self):
        """unified_engine._build_sector_for_stock 消费修正后的映射：
        主题展示名不在 sector_map（真实板块名）→ 落到主题状态。"""
        from src.orchestrator.unified_engine import _build_sector_for_stock
        stock_sector = {"688008": "存储", "600000": "银行"}
        stock_sector_status = {"688008": "retreating", "600000": "main_trend"}
        # sector_map 是真实板块名→状态；主题名不命中 → 走主题状态
        assert _build_sector_for_stock(
            "688008", {"电子化学品": "rotational"}, stock_sector, stock_sector_status
        ) == "retreating"
        # 未修正股票保持原链路：银行在 sector_map 有明确状态
        assert _build_sector_for_stock(
            "600000", {"银行": "main_trend"}, stock_sector, stock_sector_status
        ) == "main_trend"
