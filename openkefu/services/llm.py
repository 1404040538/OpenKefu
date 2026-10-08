from __future__ import annotations

import time
from typing import Any

from openai import OpenAI

from openkefu.web.config import LLMConfig

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_BASE_DELAY = 1.0


class LLMClient:
    def __init__(self, config: LLMConfig):
        if not config.api_key:
            raise RuntimeError("缺少大模型 API Key，请在 config.local.json 的 llm.api_key 或环境变量中配置")
        self.config = config
        self._client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        # thinking 禁用是 DeepSeek 方言；其他供应商可能对未知字段报 400。
        # 默认仅对 DeepSeek 生效，可用 llm.disable_thinking 显式覆盖。
        if config.disable_thinking is None:
            self._disable_thinking = "deepseek" in (config.base_url or "").lower()
        else:
            self._disable_thinking = config.disable_thinking

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def api_key(self) -> str:
        return self.config.api_key

    @property
    def base_url(self) -> str:
        return self.config.base_url

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str | None = None,
        timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        kwargs = self._build_kwargs(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
            timeout=timeout if timeout is not None else self.config.timeout_seconds,
        )
        content = self._call_with_retry(kwargs, max_retries=max_retries)
        if not content or not content.strip():
            raise RuntimeError("大模型返回空回复")
        return content.strip()

    def _build_kwargs(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None,
        temperature: float,
        max_tokens: int,
        response_format: str | None,
        stream: bool,
        timeout: float,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            "timeout": timeout,
        }
        if self._disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if response_format:
            kwargs["response_format"] = {"type": response_format}
        return kwargs

    def _call_with_retry(self, kwargs: dict[str, Any], *, max_retries: int) -> str:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content and content.strip():
                    return content.strip()
                raise RuntimeError("大模型返回空回复")
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = DEFAULT_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
        raise last_error or RuntimeError("LLM 调用失败")


_llm_clients: dict[str, LLMClient] = {}


def get_llm_client(config: LLMConfig) -> LLMClient:
    key = f"{config.api_key}|{config.base_url}|{config.model}"
    if key not in _llm_clients:
        _llm_clients[key] = LLMClient(config)
    return _llm_clients[key]
