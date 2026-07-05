"""LLM 客户端抽象（OpenAI 兼容 / Mock 模式）。

Agent 通过此客户端获取结构化 JSON 输出，用于审查分析与 patch 生成。
系统输出的是证据化审查结果，而不是模型的私有思考链（SPEC 1.3）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    pass


class LLMResponse:
    """LLM 响应封装。"""

    def __init__(self, content: str, usage: dict[str, int] | None = None,
                 raw: Any = None, elapsed_ms: int = 0) -> None:
        self.content = content
        self.usage = usage or {}
        self.raw = raw
        self.elapsed_ms = elapsed_ms

    def parse_json(self) -> Any:
        """尝试从响应中提取 JSON（容忍 ```json 围栏）。"""
        text = self.content.strip()
        if text.startswith("```"):
            # 去除围栏
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # 尝试找到第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 输出无法解析为 JSON: {exc}\n原始内容: {self.content[:300]}") from exc


class LLMClient:
    """LLM 客户端接口。"""

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2, max_tokens: int | None = None) -> LLMResponse: ...


class OpenAIClient(LLMClient):
    """OpenAI 兼容 API 客户端。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_base = settings.llm_api_base
        self._api_key = settings.llm_api_key
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds
        self._max_tokens = settings.llm_max_tokens
        if not self._api_key:
            raise LLMError("未配置 LLM_API_KEY")

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2, max_tokens: int | None = None) -> LLMResponse:
        from openai import OpenAI

        client = OpenAI(base_url=self._api_base, api_key=self._api_key, timeout=self._timeout)
        start = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            # 未显式指定时使用配置的预算（为推理模型留足思考+正文空间）。
            "max_tokens": max_tokens or self._max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        elapsed = int((time.monotonic() - start) * 1000)
        content = resp.choices[0].message.content or ""
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        logger.info("llm.chat", model=self._model, elapsed_ms=elapsed, usage=usage)
        return LLMResponse(content=content, usage=usage, raw=resp, elapsed_ms=elapsed)


class MockLLMClient(LLMClient):
    """Mock LLM 客户端：不调用真实 API，返回占位结构化结果。

    用于无 API Key 的开发/测试环境，以及 CI。
    """

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             temperature: float = 0.2, max_tokens: int | None = None) -> LLMResponse:
        logger.info("llm.mock_call", user_len=len(user))
        # 根据系统提示词返回最小化的合法结构
        if "plan" in system.lower() and "review" in system.lower():
            content = json.dumps({
                "language_profile": ["python"],
                "changed_files": [],
                "tasks": [],
                "sandbox_required": False,
                "estimated_cost_class": "low",
                "skip_reason": "mock模式：无实际 LLM 分析",
            })
        elif "security" in system.lower():
            content = json.dumps({"issues": []})
        elif "bug" in system.lower():
            content = json.dumps({"issues": []})
        elif "patch" in system.lower():
            content = json.dumps({"diff": "", "strategy": "", "risk": "low", "reasoning": "mock"})
        else:
            content = "{}"
        return LLMResponse(content=content, usage={"total_tokens": 0}, elapsed_ms=0)


_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        settings = get_settings()
        if settings.llm_mode == "mock":
            _default_client = MockLLMClient()
            logger.info("llm.mode", mode="mock")
        else:
            try:
                _default_client = OpenAIClient()
                logger.info("llm.mode", mode="openai", model=settings.llm_model)
            except LLMError:
                logger.warning("llm.openai_unavailable_fallback_mock")
                _default_client = MockLLMClient()
    return _default_client


def reset_llm_client() -> None:
    global _default_client
    _default_client = None
