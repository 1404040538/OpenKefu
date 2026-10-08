from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from openkefu.services.embedding import EmbedFn

logger = logging.getLogger(__name__)


MAX_CACHE_SIZE = 200
SIMILARITY_THRESHOLD = 0.92
CACHE_TTL_SECONDS = 3600
MAX_CACHE_SCAN = 40
TEXT_PRE_FILTER_RATIO = 0.55


class ReplyCache:
    def __init__(self, embed_fn: EmbedFn | None):
        # embed_fn 必须基于 embedding 配置构建（见 build_embed_fn），
        # 不能复用 LLM 客户端——LLM 供应商未必提供 embeddings 接口。
        self._embed = embed_fn
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, query: str, shop_id: int) -> dict[str, Any] | None:
        query = query.strip()
        if not query:
            return None
        with self._lock:
            self._expire_stale()
            entries = list(self._entries.items())
        query_clean = _normalize(query)
        scanned = 0
        candidates: list[tuple[str, float, dict[str, Any], str]] = []
        for cache_key, (cached_at, entry) in entries:
            if entry.get("_cache_shop_id") != shop_id:
                continue
            if scanned >= MAX_CACHE_SCAN:
                break
            scanned += 1
            entry_query = str(entry.get("_cache_query") or "")
            if query_clean == _normalize(entry_query):
                return self._confirm(cache_key, cached_at, entry)
            if len(query_clean) < 20 and len(_normalize(entry_query)) < 20:
                continue
            if self._embed is None:
                continue
            # 轻量文本相似度粗筛，避免无谓的 embedding 调用。
            try:
                ratio = difflib.SequenceMatcher(None, query_clean, _normalize(entry_query)).ratio()
            except Exception:
                continue
            if ratio >= TEXT_PRE_FILTER_RATIO:
                candidates.append((cache_key, cached_at, entry, _normalize(entry_query)))

        if not candidates:
            return None
        # 一次批量请求：query + 全部候选，避免逐条调用 embedding API。
        try:
            vectors = self._embed([query_clean] + [item[3] for item in candidates])
        except Exception:
            logger.debug("reply cache embedding failed", exc_info=True)
            return None
        if len(vectors) != len(candidates) + 1:
            return None
        query_vec = vectors[0]
        for (cache_key, cached_at, entry, _), entry_vec in zip(candidates, vectors[1:]):
            if _cosine_similarity(query_vec, entry_vec) >= SIMILARITY_THRESHOLD:
                return self._confirm(cache_key, cached_at, entry)
        return None

    def _confirm(self, cache_key: str, cached_at: float, entry: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            # 相似度网络请求期间条目可能已过期或被另一线程淘汰。
            if cache_key not in self._entries or time.time() - cached_at > CACHE_TTL_SECONDS:
                return None
            self._entries.move_to_end(cache_key)
        result = dict(entry)
        result["_cache_hit"] = True
        return result

    def put(self, query: str, shop_id: int, intent: dict[str, Any]) -> None:
        query = query.strip()
        if not query:
            return
        intent = dict(intent)
        intent["_cache_query"] = query
        intent["_cache_shop_id"] = shop_id
        cache_key = f"{shop_id}:{query[:80]}:{time.time()}"
        with self._lock:
            self._entries[cache_key] = (time.time(), intent)
            if len(self._entries) > MAX_CACHE_SIZE:
                self._entries.popitem(last=False)

    def _expire_stale(self) -> None:
        now = time.time()
        stale_keys = [k for k, (ts, _) in self._entries.items() if now - ts > CACHE_TTL_SECONDS]
        for k in stale_keys:
            del self._entries[k]


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    return text.lower().strip()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
