from __future__ import annotations

import difflib
import threading
import time
from collections import OrderedDict
from typing import Any

from openkefu.services.llm import LLMClient
from openkefu.web.config import EmbeddingConfig


MAX_CACHE_SIZE = 200
SIMILARITY_THRESHOLD = 0.92
CACHE_TTL_SECONDS = 3600
MAX_CACHE_SCAN = 40
TEXT_PRE_FILTER_RATIO = 0.55


class ReplyCache:
    def __init__(self, llm_client: LLMClient, embedding_config: EmbeddingConfig):
        self._llm = llm_client
        self._emb_config = embedding_config
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, query: str, shop_id: int) -> dict[str, Any] | None:
        query = query.strip()
        if not query:
            return None
        with self._lock:
            self._expire_stale()
            entries = list(self._entries.items())
        scanned = 0
        for cache_key, (cached_at, entry) in entries:
            entry_query = entry.get("_cache_query") or ""
            entry_shop = entry.get("_cache_shop_id")
            if entry_shop != shop_id:
                continue
            if scanned >= MAX_CACHE_SCAN:
                break
            scanned += 1
            if self._is_similar(query, entry_query):
                with self._lock:
                    # 相似度网络请求期间条目可能已过期或被另一线程淘汰。
                    if cache_key not in self._entries or time.time() - cached_at > CACHE_TTL_SECONDS:
                        continue
                    self._entries.move_to_end(cache_key)
                result = dict(entry)
                result["_cache_hit"] = True
                return result
        return None

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

    def _is_similar(self, a: str, b: str) -> bool:
        if a == b:
            return True
        a_clean = _normalize(a)
        b_clean = _normalize(b)
        if a_clean == b_clean:
            return True
        if len(a_clean) < 20 and len(b_clean) < 20:
            return False
        # 先用轻量文本相似度粗筛，过滤明显不相关的候选，
        # 避免对每条缓存都发起 embedding API 调用。
        try:
            if difflib.SequenceMatcher(None, a_clean, b_clean).ratio() < TEXT_PRE_FILTER_RATIO:
                return False
        except Exception:
            pass
        try:
            emb_a = self._llm.embed([a_clean], model=self._emb_config.model)
            emb_b = self._llm.embed([b_clean], model=self._emb_config.model)
            similarity = _cosine_similarity(emb_a[0], emb_b[0])
            return similarity >= SIMILARITY_THRESHOLD
        except Exception:
            return False

    def _expire_stale(self) -> None:
        now = time.time()
        stale_keys = [k for k, (ts, _) in self._entries.items() if now - ts > CACHE_TTL_SECONDS]
        for k in stale_keys:
            del self._entries[k]


def _normalize(text: str) -> str:
    import re
    text = re.sub(r"\s+", "", text)
    text = text.lower()
    return text.strip()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
