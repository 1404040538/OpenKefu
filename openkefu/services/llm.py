from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Generator

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
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> str:
        kwargs = self._build_kwargs(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
            timeout=timeout,
        )
        content = self._call_with_retry(kwargs, max_retries=max_retries)
        if not content or not content.strip():
            raise RuntimeError("大模型返回空回复")
        return content.strip()

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Generator[str, None, None]:
        kwargs = self._build_kwargs(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=None,
            stream=True,
            timeout=timeout,
        )
        response = self._client.chat.completions.create(**kwargs)
        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def embed(self, texts: list[str], *, model: str | None = None, timeout: float = 60) -> list[list[float]]:
        emb_model = model or self.config.model
        response = self._client.embeddings.create(model=emb_model, input=texts, timeout=timeout)
        return [list(item.embedding) for item in response.data]

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
            "extra_body": {"thinking": {"type": "disabled"}},
        }
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


@lru_cache(maxsize=8)
def _client_from_config(api_key: str, base_url: str, model: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


_llm_clients: dict[str, LLMClient] = {}


def get_llm_client(config: LLMConfig) -> LLMClient:
    key = f"{config.api_key}|{config.base_url}|{config.model}"
    if key not in _llm_clients:
        _llm_clients[key] = LLMClient(config)
    return _llm_clients[key]
