from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from openkefu.web.server_status import (
    ServerStatusCollector,
    cleanup_server_status,
    latest_server_status,
    server_status_history,
)


class FakeDb:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []
        self.executes = []

    def query(self, sql, params=None):
        self.queries.append((sql, tuple(params or ())))
        return list(self.rows)

    def execute(self, sql, params=None):
        self.executes.append((sql, tuple(params or ())))
        return 1


class FakeHub:
    def publish(self, event):
        return None


class FakeLogger:
    def log(self, *args, **kwargs):
        return None


class FakeProc:
    def __init__(self, **info):
        self.info = info


class FakePsutil:
    def __init__(self):
        self.disk_counter = SimpleNamespace(read_bytes=1000, write_bytes=2000)
        self.net_counter = SimpleNamespace(bytes_recv=3000, bytes_sent=4000)

    def cpu_percent(self, interval=None):
        return 12.5

    def cpu_count(self):
        return 8

    def virtual_memory(self):
        return SimpleNamespace(total=1000, used=650, percent=65.0)

    def swap_memory(self):
        return SimpleNamespace(total=500, used=100, percent=20.0)

    def disk_usage(self, path):
        return SimpleNamespace(total=2000, used=1000, percent=50.0)

    def disk_io_counters(self):
        return self.disk_counter

    def net_io_counters(self):
        return self.net_counter

    def boot_time(self):
        return 1000

    def net_connections(self, kind="inet"):
        return [1, 2, 3]

    def disk_partitions(self, all=False):
        return [SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4")]

    def process_iter(self, attrs):
        return [
            FakeProc(pid=1, name="idle", username="root", cpu_percent=0.1, memory_percent=0.2),
            FakeProc(pid=2, name="busy", username="app", cpu_percent=32.0, memory_percent=5.5),
        ]


def fake_config():
    return SimpleNamespace(
        runtime=SimpleNamespace(role="api"),
        server_status=SimpleNamespace(
            enabled=True,
            collect_interval_seconds=5,
            persist_interval_seconds=30,
            raw_retention_days=7,
            rollup_retention_days=30,
            top_process_limit=1,
        ),
    )


class ServerStatusTest(unittest.TestCase):
    def test_collect_sample_normalizes_core_metrics(self):
        collector = ServerStatusCollector(FakeDb(), FakeHub(), FakeLogger(), fake_config(), psutil_module=FakePsutil())
        sample = collector.collect_sample()

        self.assertEqual(sample["cpu_percent"], 12.5)
        self.assertEqual(sample["cpu_count"], 8)
        self.assertEqual(sample["memory_percent"], 65.0)
        self.assertEqual(sample["swap_percent"], 20.0)
        self.assertEqual(sample["disk_percent"], 50.0)
        self.assertEqual(sample["connection_count"], 3)
        self.assertEqual(sample["partitions"][0]["mountpoint"], "/")
        self.assertEqual(sample["top_processes"][0]["name"], "busy")
        self.assertEqual(len(sample["top_processes"]), 1)

    def test_rate_pair_uses_positive_delta_per_second(self):
        previous = (10.0, SimpleNamespace(read_bytes=100, write_bytes=200))
        current = SimpleNamespace(read_bytes=250, write_bytes=500)

        read_bps, write_bps = ServerStatusCollector._rate_pair(previous, 13.0, current, ("read_bytes", "write_bytes"))

        self.assertEqual(read_bps, 50.0)
        self.assertEqual(write_bps, 100.0)

    def test_latest_server_status_marks_fresh_payload(self):
        db = FakeDb([
            {
                "node_id": "host-a",
                "hostname": "host-a",
                "pid": 123,
                "status": "online",
                "latest_json": '{"cpu_percent": 10}',
                "last_error": "",
                "heartbeat_at": datetime.now(),
                "lease_expires_at": datetime.now() + timedelta(seconds=10),
            }
        ])

        result = latest_server_status(db)

        self.assertFalse(result["stale"])
        self.assertEqual(result["sample"]["cpu_percent"], 10)

    def test_latest_server_status_marks_stale_payload(self):
        db = FakeDb([
            {
                "node_id": "host-a",
                "hostname": "host-a",
                "pid": 123,
                "status": "online",
                "latest_json": "{}",
                "last_error": "late",
                "heartbeat_at": datetime.now() - timedelta(minutes=5),
                "lease_expires_at": datetime.now() - timedelta(minutes=4),
            }
        ])

        result = latest_server_status(db)

        self.assertTrue(result["stale"])
        self.assertEqual(result["last_error"], "late")

    def test_history_uses_raw_snapshots_for_short_ranges(self):
        db = FakeDb([])

        result = server_status_history(db, "1h")

        self.assertEqual(result["range"], "1h")
        self.assertIn("server_status_snapshots", db.queries[0][0])
        self.assertIn("bucketed", db.queries[0][0])
        self.assertIn("GROUP BY bucket_epoch", db.queries[0][0])

    def test_history_uses_hourly_rollups_for_30d(self):
        db = FakeDb([])

        result = server_status_history(db, "30d")

        self.assertEqual(result["range"], "30d")
        self.assertIn("server_status_rollups_hourly", db.queries[0][0])

    def test_cleanup_applies_both_retention_windows(self):
        db = FakeDb([])

        cleanup_server_status(db, raw_retention_days=7, rollup_retention_days=30)

        self.assertEqual(len(db.executes), 2)
        self.assertIn("server_status_snapshots", db.executes[0][0])
        self.assertIn("server_status_rollups_hourly", db.executes[1][0])


if __name__ == "__main__":
    unittest.main()
