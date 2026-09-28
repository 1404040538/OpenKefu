from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from openkefu.web.db import Database


class DatabasePoolRecoveryTest(unittest.TestCase):
    def make_db(self) -> Database:
        return Database(SimpleNamespace(mysql=SimpleNamespace(pool_size=2)))

    def test_failed_new_connections_do_not_consume_pool_capacity(self):
        db = self.make_db()
        db._connect = Mock(side_effect=OSError("database temporarily unavailable"))

        for _ in range(4):
            with self.assertRaisesRegex(OSError, "temporarily unavailable"):
                db._pooled_connect()
            self.assertEqual(db._pool_created, 0)

        first, second = Mock(), Mock()
        db._connect.side_effect = [first, second]
        self.assertEqual(db._pooled_connect(), (first, True))
        self.assertEqual(db._pooled_connect(), (second, True))
        self.assertEqual(db._pool_created, 2)
        db._release_pooled(first, True)
        db._release_pooled(second, True)

    def test_failed_replacement_returns_slot_and_recovers_after_outage(self):
        db = self.make_db()
        stale = Mock()
        db._connect = Mock(return_value=stale)
        conn, pooled = db._pooled_connect()
        db._release_pooled(conn, pooled)
        stale.ping.side_effect = OSError("connection lost")
        db._connect.side_effect = OSError("database still unavailable")

        with self.assertRaisesRegex(OSError, "still unavailable"):
            db._pooled_connect()

        stale.close.assert_called_once()
        self.assertEqual(db._pool_created, 0)
        recovered = Mock()
        db._connect.side_effect = None
        db._connect.return_value = recovered
        self.assertEqual(db._pooled_connect(), (recovered, True))
        db._release_pooled(recovered, True)
        self.assertEqual(db._pooled_connect(), (recovered, True))
        recovered.ping.assert_called_once_with(reconnect=False)

    def test_slow_connection_does_not_block_other_available_pool_slot(self):
        db = self.make_db()
        first_started = threading.Event()
        release_first = threading.Event()
        first, second = Mock(), Mock()

        def connect(*, database=True):
            if threading.current_thread().name.endswith("_0"):
                first_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("test failed to release connection attempt")
                return first
            return second

        db._connect = Mock(side_effect=connect)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pool-test") as executor:
            pending = executor.submit(db._pooled_connect)
            try:
                self.assertTrue(first_started.wait(2))
                concurrent = executor.submit(db._pooled_connect)
                self.assertEqual(concurrent.result(timeout=2), (second, True))
                self.assertEqual(db._pool_created, 2)
            finally:
                release_first.set()
            self.assertEqual(pending.result(timeout=2), (first, True))
        db._release_pooled(first, True)
        db._release_pooled(second, True)


if __name__ == "__main__":
    unittest.main()
