from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from openkefu.platforms.pdd.chat.reply_cache import ReplyCache


CACHED_QUERY = "I would like to ask about delivery time to Shanghai tomorrow"
NEW_QUERY = "I would like to ask about delivery time to Shanghai today"


class BlockingEmbedding:
    def __init__(self, *, matching: bool):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.matching = matching
        self.calls = 0

    def embed(self, texts, *, model):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test did not release embedding request")
            return [[1.0, 0.0]]
        return [[1.0, 0.0] if self.matching else [0.0, 1.0]]


class ReplyCacheConcurrencyTest(unittest.TestCase):
    def make_cache(self, *, matching=False):
        embedding = BlockingEmbedding(matching=matching)
        cache = ReplyCache(embedding, SimpleNamespace(model="fake-no-network"))
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
        with patch("openkefu.platforms.pdd.chat.reply_cache.MAX_CACHE_SIZE", 2):
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


if __name__ == "__main__":
    unittest.main()
