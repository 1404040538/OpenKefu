from __future__ import annotations

from typing import Callable

from openai import OpenAI

from openkefu.web.config import EmbeddingConfig

EmbedFn = Callable[[list[str]], list[list[float]]]


def embedding_configured(config: EmbeddingConfig) -> bool:
    return bool(config.api_key and config.base_url and config.model)


def build_embed_fn(config: EmbeddingConfig) -> EmbedFn | None:
    """基于 embedding 配置构建批量向量化函数；配置不完整时返回 None。

    注意：必须使用 embedding 自己的 base_url/api_key（与 llm 配置无关），
    否则在 LLM 供应商不提供 embeddings 接口时调用会静默失败。
    """
    if not embedding_configured(config):
        return None
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    def embed(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(model=config.model, input=texts)
        return [list(item.embedding) for item in response.data]

    return embed
