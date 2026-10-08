from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from openkefu.platforms.pdd.chat.reply_cache import ReplyCache
from openkefu.services.embedding import build_embed_fn
from openkefu.web.config import EmbeddingConfig


CACHED_QUERY = "I would like to ask about delivery time to Shanghai tomorrow"
NEW_QUERY = "I would like to ask about delivery time to Shanghai today"


class BlockingEmbedding:
    """模拟批量向量化接口：blocking=True 时第一次调用阻塞等待测试放行。"""

    def __init__(self, *, matching: bool, blocking: bool = True):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.matching = matching
        self.blocking = blocking
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        if self.blocking and self.calls == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test did not release embedding request")
        vectors = [[1.0, 0.0]]
        for _ in texts[1:]:
            vectors.append([1.0, 0.0] if self.matching else [0.0, 1.0])
        return vectors


class ReplyCacheConcurrencyTest(unittest.TestCase):
    def make_cache(self, *, matching=False, blocking=True):
        embedding = BlockingEmbedding(matching=matching, blocking=blocking)
        cache = ReplyCache(embedding)
        cache.put(CACHED_QUERY, 1, {"reply": "first"})
        cache.put("another initial cache entry", 1, {"reply": "second"})
        return cache, embedding

    def test_concurrent_write_during_embedding_does_not_break_iteration(self):
        cache, embedding = self.make_cache()
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = executor.submit(cache.get, NEW_QUERY, 1)
            try:
                self.assertTrue(embedding.entered.wait(2))
                # The write must complete before the network request: no lock held
                # over embedding, and no live OrderedDict iterator exposed to it.
                writer = executor.submit(cache.put, "new buyer question", 1, {"reply": "third"})
                writer.result(timeout=2)
            finally:
                embedding.release.set()
            self.assertIsNone(result.result(timeout=2))
        self.assertEqual(cache.get("new buyer question", 1)["reply"], "third")

    def test_entry_evicted_during_embedding_is_not_returned(self):
        with patch("openkefu.services.reply_cache.MAX_CACHE_SIZE", 2):
            cache, embedding = self.make_cache(matching=True)
            with ThreadPoolExecutor(max_workers=2) as executor:
                result = executor.submit(cache.get, NEW_QUERY, 1)
                try:
                    self.assertTrue(embedding.entered.wait(2))
                    writer = executor.submit(cache.put, "new buyer question", 1, {"reply": "third"})
                    writer.result(timeout=2)
                finally:
                    embedding.release.set()
                self.assertIsNone(result.result(timeout=2))

    def test_semantic_match_hits_entry(self):
        cache, embedding = self.make_cache(matching=True, blocking=False)
        hit = cache.get(NEW_QUERY, 1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["reply"], "first")
        self.assertTrue(hit["_cache_hit"])
        # 一次批量请求应同时携带 query 与全部粗筛候选。
        self.assertEqual(embedding.calls, 1)

    def test_shop_isolation(self):
        cache, _ = self.make_cache(matching=True)
        self.assertIsNone(cache.get(CACHED_QUERY, 2))

    def test_missing_embed_fn_degrades_to_exact_match(self):
        cache = ReplyCache(None)
        cache.put(CACHED_QUERY, 1, {"reply": "first"})
        self.assertEqual(cache.get(CACHED_QUERY, 1)["reply"], "first")
        self.assertIsNone(cache.get(NEW_QUERY, 1))


class BuildEmbedFnTest(unittest.TestCase):
    def test_incomplete_config_returns_none(self):
        self.assertIsNone(build_embed_fn(EmbeddingConfig(api_key="", base_url="", model="")))
        self.assertIsNone(build_embed_fn(EmbeddingConfig(api_key="k", base_url="", model="m")))
        self.assertIsNone(build_embed_fn(EmbeddingConfig(api_key="k", base_url="https://x", model="")))

    def test_configured_returns_callable(self):
        fn = build_embed_fn(EmbeddingConfig(api_key="k", base_url="https://x", model="m"))
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
