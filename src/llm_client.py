"""
LLM 统一客户端
支持多提供商切换（DeepSeek / OpenAI / Ollama）
基于 OpenAI 兼容接口标准
"""
import logging
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"


@dataclass
class LLMMessage:
    role: str  # system / user / assistant
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: Dict[str, int] = field(default_factory=dict)
    raw: Optional[Dict] = None


class LLMClient:
    """统一LLM客户端，支持多提供商切换"""

    def __init__(self, config_path: Optional[str] = None):
        self._config_path = config_path or str(CONFIG_DIR / "llm.yaml")
        self._config: Dict[str, Any] = {}
        self._active_provider: str = ""
        self._client = None
        self._prompts: Dict[str, Dict] = {}
        self._load_config()

    def _load_config(self):
        """加载LLM配置"""
        with open(self._config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # 支持 llm: 作为顶层key的YAML结构
        self._config = raw.get("llm", raw)
        self._active_provider = self._config.get("active", "deepseek")
        self._prompts = self._config.get("prompts", {})
        logger.info("LLM config loaded, active provider: %s", self._active_provider)

    def _get_client(self, provider: Optional[str] = None):
        """获取指定provider的OpenAI兼容客户端"""
        provider = provider or self._active_provider
        providers = self._config.get("providers", {})

        if provider not in providers:
            raise ValueError(f"Provider '{provider}' not found in config. Available: {list(providers.keys())}")

        cfg = providers[provider]
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=cfg.get("api_key", "sk-dummy"),
                base_url=cfg.get("base_url"),
            )
            return client, cfg
        except ImportError:
            logger.warning("openai package not installed, using requests fallback")
            return None, cfg

    def switch_provider(self, provider: str):
        """运行时切换LLM提供商"""
        providers = self._config.get("providers", {})
        if provider not in providers:
            raise ValueError(f"Provider '{provider}' not available. Available: {list(providers.keys())}")
        self._active_provider = provider
        self._client = None  # 重置客户端
        logger.info("Switched LLM provider to: %s", provider)

    def chat(
        self,
        messages: List[LLMMessage],
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        发送聊天请求

        Args:
            messages: 消息列表
            provider: 指定provider（不传用active）
            temperature: 覆盖默认温度
            max_tokens: 覆盖默认max_tokens
        """
        provider = provider or self._active_provider
        client, cfg = self._get_client(provider)

        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]

        request_params = {
            "model": cfg.get("model", "gpt-4o"),
            "messages": msg_dicts,
            "temperature": temperature or cfg.get("temperature", 0.3),
            "max_tokens": max_tokens or cfg.get("max_tokens", 4096),
        }
        request_params.update(kwargs)

        if client is not None:
            return self._chat_openai(client, request_params, provider)
        else:
            return self._chat_requests(cfg, request_params, provider)

    def _chat_openai(self, client, params: Dict, provider: str) -> LLMResponse:
        """使用openai库发送请求"""
        try:
            response = client.chat.completions.create(**params)
            content = response.choices[0].message.content
            usage = {}
            if hasattr(response, "usage") and response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }
            return LLMResponse(
                content=content,
                model=params["model"],
                provider=provider,
                usage=usage,
                raw=response.model_dump() if hasattr(response, "model_dump") else None,
            )
        except Exception as e:
            logger.error("LLM request failed (openai): %s", e)
            raise

    def _chat_requests(self, cfg: Dict, params: Dict, provider: str) -> LLMResponse:
        """使用requests库作为fallback"""
        import requests

        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        try:
            resp = requests.post(url, json=params, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return LLMResponse(
                content=content,
                model=params["model"],
                provider=provider,
                usage=usage,
                raw=data,
            )
        except Exception as e:
            logger.error("LLM request failed (requests): %s", e)
            raise

    def chat_with_prompt(
        self,
        prompt_name: str,
        user_content: str,
        temperature: Optional[float] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        使用预定义prompt模板发送请求

        Args:
            prompt_name: prompts配置中的模板名称
            user_content: 用户输入内容
            temperature: 覆盖模板温度
        """
        prompt_cfg = self._prompts.get(prompt_name)
        if not prompt_cfg:
            raise ValueError(f"Prompt template '{prompt_name}' not found. Available: {list(self._prompts.keys())}")

        messages = [
            LLMMessage(role="system", content=prompt_cfg["system"]),
            LLMMessage(role="user", content=user_content),
        ]

        temp = temperature or prompt_cfg.get("temperature")
        return self.chat(messages, temperature=temp, **kwargs)

    def extract_json(self, response: LLMResponse) -> Any:
        """
        从LLM响应中提取JSON内容
        支持纯JSON、Markdown代码块包裹的JSON
        """
        content = response.content.strip()

        # 尝试提取markdown代码块中的JSON
        if "```json" in content:
            start = content.index("```json") + 7
            end = content.index("```", start)
            content = content[start:end].strip()
        elif "```" in content:
            start = content.index("```") + 3
            end = content.index("```", start)
            content = content[start:end].strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response as JSON: %s", e)
            logger.debug("Raw content: %s", content[:500])
            return None

    def extract_judgments(self, article_text: str) -> Optional[Dict]:
        """
        从文章中提取可追踪的判断（观点挖掘场景）

        Args:
            article_text: 用户粘贴的文章原文

        Returns:
            结构化判断结果
        """
        response = self.chat_with_prompt("judgment_extraction", article_text)
        result = self.extract_json(response)

        if result is None:
            logger.warning("Failed to extract structured judgments from LLM response")
            return None

        return result

    def analyze_news(self, news_text: str) -> Optional[Dict]:
        """
        分析新闻对个股/板块的影响

        Args:
            news_text: 新闻文本

        Returns:
            影响分析结果
        """
        response = self.chat_with_prompt("news_analysis", news_text)
        return self.extract_json(response)

    def analyze_financial_report(self, report_text: str) -> Optional[Dict]:
        """
        解读财报数据

        Args:
            report_text: 财报关键数据文本

        Returns:
            财报解读结果
        """
        response = self.chat_with_prompt("financial_report_analysis", report_text)
        return self.extract_json(response)


# 单例模式
_instance: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取LLM客户端单例"""
    global _instance
    if _instance is None:
        _instance = LLMClient()
    return _instance


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = get_llm_client()
    print(f"Active provider: {client._active_provider}")
    print(f"Available providers: {list(client._config.get('providers', {}).keys())}")
    print(f"Available prompts: {list(client._prompts.keys())}")
