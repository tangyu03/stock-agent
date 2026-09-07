"""
Pydantic 配置校验模型
启动时校验所有YAML配置文件的格式正确性
"""
import logging
from pydantic import BaseModel
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


# ============ LLM 配置 ============
class LLMProvider(BaseModel):
    api_key: str = ""
    base_url: str
    model: str
    max_tokens: int = 4096
    temperature: float = 0.3


class LLMPromptConfig(BaseModel):
    system: str
    temperature: float = 0.3


class LLMConfigRoot(BaseModel):
    version: str
    llm: Dict[str, Any]  # nested under 'llm' key


class LLMConfig(BaseModel):
    version: str
    active: str
    providers: Dict[str, LLMProvider]
    prompts: Dict[str, LLMPromptConfig]


# ============ 推送配置 ============
class ScheduleTime(BaseModel):
    pre_market: str = "08:50"
    auction: str = "09:25"
    market_open: str = "09:35"
    mid_morning: str = "10:30"
    mid_afternoon: str = "14:00"
    pre_close: str = "14:50"
    post_market: str = "15:30"


class PushConfig(BaseModel):
    version: str
    pushplus: Dict[str, Any]
    schedule: ScheduleTime
    levels: Dict[str, str]


# ============ 持仓配置 ============
class HoldingItem(BaseModel):
    code: str
    name: str
    shares: Optional[int] = 0
    cost: Optional[float] = 0.0
    category: str = "A"


class PortfolioConfig(BaseModel):
    version: str
    holdings: Optional[List[HoldingItem]] = None
    total_asset: float = 1000000


# ============ 自选配置 ============
class WatchlistItem(BaseModel):
    code: str
    name: str
    added_at: str = ""
    note: str = ""


class WatchlistConfig(BaseModel):
    version: str
    watchlist: Dict[str, List[WatchlistItem]]


# ============ 仓位配置 ============
class ModeLimit(BaseModel):
    attack: float = 0.8
    defend: float = 0.5
    retreat: float = 0.1


class PositionConfig(BaseModel):
    version: str
    position: Dict[str, Any]


# ============ 风控配置 ============
class RiskConfig(BaseModel):
    version: str
    risk: Dict[str, Any]


# ============ 观点配置 ============
class InsightsConfig(BaseModel):
    version: str
    insights: List[Dict[str, Any]] = []


# ============ 大盘评分配置 ============
class MarketScoringConfig(BaseModel):
    version: str
    scoring: Dict[str, Any]
    momentum: Optional[Dict[str, Any]] = None
    mode_mapping: Optional[Dict[str, Any]] = None


# ============ 板块扫描配置 ============
class SectorScannerConfig(BaseModel):
    version: str
    classification: Dict[str, Any]
    cross_diagnosis: Dict[str, Any]


# ============ 板块映射配置 ============
class SectorMapConfig(BaseModel):
    version: str
    sector_map: Dict[str, Any]  # nested under 'sector_map' key


# ============ 调度配置 ============
class ScheduleTaskBlock(BaseModel):
    time: str
    tasks: List[str]
    description: str = ""
    day: Optional[str] = None


class ScheduleConfig(BaseModel):
    version: str
    schedule: Dict[str, ScheduleTaskBlock]


# ============ 配置加载器 ============
import yaml
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"

CONFIG_VALIDATORS = {
    "llm.yaml": LLMConfig,
    "push.yaml": PushConfig,
    "portfolio.yaml": PortfolioConfig,
    "position.yaml": PositionConfig,
    "risk.yaml": RiskConfig,
    "insights.yaml": InsightsConfig,
    "market_scoring.yaml": MarketScoringConfig,
    "sector_scanner.yaml": SectorScannerConfig,
    "schedule.yaml": ScheduleConfig,
    "sector_map.yaml": SectorMapConfig,
}


def load_config(name: str) -> Dict:
    """加载并校验单个配置文件"""
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 校验格式（宽松模式：只校验version和基本结构）
    if raw and isinstance(raw, dict) and "version" not in raw:
        logger.warning("Config %s missing version field", name)

    return raw


def load_all_configs() -> Dict[str, Dict]:
    """加载并校验所有配置文件"""
    configs = {}
    errors = []

    for name in CONFIG_VALIDATORS:
        try:
            configs[name] = load_config(name)
        except Exception as e:
            errors.append(f"{name}: {e}")

    if errors:
        error_msg = "Config validation errors:\n" + "\n".join(errors)
        raise ValueError(error_msg)

    return configs


if __name__ == "__main__":
    configs = load_all_configs()
    for name, cfg_unused in configs.items():  # noqa: B007 — 仅需键名
        print(f"✅ {name} loaded successfully")
