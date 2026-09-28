from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from openkefu.web.config import AppConfig
from openkefu.web.db import Database, json_dumps
from openkefu.web.realtime import RealtimeHub, RuntimeLogger

try:
    import psutil
except Exception:  # pragma: no cover - exercised only when dependency is absent.
    psutil = None


logger = logging.getLogger(__name__)


HISTORY_RANGES = {
    "1h": (timedelta(hours=1), 30),
    "6h": (timedelta(hours=6), 60),
    "24h": (timedelta(hours=24), 300),
    "7d": (timedelta(days=7), 1800),
    "30d": (timedelta(days=30), 3600),
}


class ServerStatusCollector:
    def __init__(
        self,
        db: Database,
        hub: RealtimeHub,
        runtime_logger: RuntimeLogger,
        config: AppConfig,
        *,
        psutil_module: Any | None = None,
    ):
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.config = config
        self.psutil = psutil_module if psutil_module is not None else psutil
        self.hostname = socket.gethostname()
        self.node_id = self.hostname
        self.pid = os.getpid()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_disk_io: tuple[float, Any] | None = None
        self._previous_net_io: tuple[float, Any] | None = None
        self._last_persist_at = 0.0
        self._last_rollup_hour: datetime | None = None

    def start(self) -> None:
        if not self.config.server_status.enabled:
            return
        if self.config.runtime.role == "worker":
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="server-status-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        if not self.psutil:
            self._write_collector_error("psutil is not installed")
            return
        interval = int(self.config.server_status.collect_interval_seconds)
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                if self._acquire_lease():
                    sample = self.collect_sample()
                    self._write_latest(sample, last_error=None)
                    now = time.monotonic()
                    if now - self._last_persist_at >= int(self.config.server_status.persist_interval_seconds):
                        self._persist_snapshot(sample)
                        self._last_persist_at = now
                    self._maybe_rollup_and_cleanup()
                    self.hub.publish({"type": "server_status", "data": latest_server_status(self.db)})
            except Exception as exc:
                logger.exception("server status collection failed")
                self._write_collector_error(str(exc))
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "server_status.collect",
                    "server status collection failed",
                    error=exc,
                )
            elapsed = time.monotonic() - started_at
            self._stop_event.wait(max(1.0, interval - elapsed))

    def _acquire_lease(self) -> bool:
        lease_seconds = max(15, int(self.config.server_status.collect_interval_seconds) * 3)
        expires_at = datetime.now() + timedelta(seconds=lease_seconds)
        self.db.execute(
            """
            INSERT INTO server_status_collectors
            (node_id, hostname, pid, heartbeat_at, lease_expires_at, status)
            VALUES (%s,%s,%s,NOW(),%s,'starting')
            ON DUPLICATE KEY UPDATE hostname=VALUES(hostname)
            """,
            (self.node_id, self.hostname, self.pid, expires_at),
        )
        updated = self.db.execute(
            """
            UPDATE server_status_collectors
            SET pid=%s,
                heartbeat_at=NOW(),
                lease_expires_at=%s,
                status='online'
            WHERE node_id=%s
              AND (lease_expires_at<NOW() OR pid=%s OR status<>'online')
            """,
            (self.pid, expires_at, self.node_id, self.pid),
        )
        return updated > 0

    def collect_sample(self) -> dict[str, Any]:
        ps = self.psutil
        assert ps is not None
        collected_at = datetime.now()
        cpu_percent = float(ps.cpu_percent(interval=None))
        cpu_count = int(ps.cpu_count() or 0)
        load1, load5, load15 = _loadavg()
        memory = ps.virtual_memory()
        swap = ps.swap_memory()
        disk = ps.disk_usage("/")
        disk_io = ps.disk_io_counters()
        net_io = ps.net_io_counters()
        now = time.monotonic()
        disk_rates = self._rate_pair(self._previous_disk_io, now, disk_io, ("read_bytes", "write_bytes"))
        net_rates = self._rate_pair(self._previous_net_io, now, net_io, ("bytes_recv", "bytes_sent"))
        self._previous_disk_io = (now, disk_io)
        self._previous_net_io = (now, net_io)

        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "pid": self.pid,
            "collected_at": collected_at.strftime("%Y-%m-%d %H:%M:%S"),
            "cpu_percent": round(cpu_percent, 2),
            "cpu_count": cpu_count,
            "load1": round(load1, 3),
            "load5": round(load5, 3),
            "load15": round(load15, 3),
            "memory_total": int(memory.total),
            "memory_used": int(memory.used),
            "memory_percent": round(float(memory.percent), 2),
            "swap_total": int(swap.total),
            "swap_used": int(swap.used),
            "swap_percent": round(float(swap.percent), 2),
            "disk_total": int(disk.total),
            "disk_used": int(disk.used),
            "disk_percent": round(float(disk.percent), 2),
            "disk_read_bps": round(disk_rates[0], 2),
            "disk_write_bps": round(disk_rates[1], 2),
            "net_recv_bps": round(net_rates[0], 2),
            "net_sent_bps": round(net_rates[1], 2),
            "connection_count": self._connection_count(),
            "uptime_seconds": max(0, int(time.time() - float(ps.boot_time()))),
            "partitions": self._partitions(),
            "top_processes": self._top_processes(),
        }

    @staticmethod
    def _rate_pair(previous: tuple[float, Any] | None, now: float, current: Any, names: tuple[str, str]) -> tuple[float, float]:
        if not previous or not current:
            return 0.0, 0.0
        prev_time, prev = previous
        elapsed = max(0.001, now - prev_time)
        first = max(0.0, float(getattr(current, names[0], 0) - getattr(prev, names[0], 0)) / elapsed)
        second = max(0.0, float(getattr(current, names[1], 0) - getattr(prev, names[1], 0)) / elapsed)
        return first, second

    def _connection_count(self) -> int:
        try:
            return len(self.psutil.net_connections(kind="inet"))
        except Exception:
            return 0

    def _partitions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for part in self.psutil.disk_partitions(all=False):
            try:
                usage = self.psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            rows.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total": int(usage.total),
                    "used": int(usage.used),
                    "percent": round(float(usage.percent), 2),
                }
            )
        return rows[:16]

    def _top_processes(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        limit = int(self.config.server_status.top_process_limit)
        for proc in self.psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
            except Exception:
                continue
            rows.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "",
                    "username": info.get("username") or "",
                    "cpu_percent": round(float(info.get("cpu_percent") or 0), 2),
                    "memory_percent": round(float(info.get("memory_percent") or 0), 2),
                }
            )
        return sorted(rows, key=lambda item: (item["cpu_percent"], item["memory_percent"]), reverse=True)[:limit]

    def _write_latest(self, sample: dict[str, Any], *, last_error: str | None) -> None:
        self.db.execute(
            """
            UPDATE server_status_collectors
            SET latest_json=%s,
                last_error=%s,
                heartbeat_at=NOW(),
                status='online'
            WHERE node_id=%s AND pid=%s
            """,
            (json_dumps(sample), last_error, self.node_id, self.pid),
        )

    def _write_collector_error(self, message: str) -> None:
        self.db.execute(
            """
            INSERT INTO server_status_collectors
            (node_id, hostname, pid, heartbeat_at, lease_expires_at, status, last_error)
            VALUES (%s,%s,%s,NOW(),NOW(),'error',%s)
            ON DUPLICATE KEY UPDATE
                hostname=VALUES(hostname),
                pid=VALUES(pid),
                heartbeat_at=NOW(),
                status='error',
                last_error=VALUES(last_error)
            """,
            (self.node_id, self.hostname, self.pid, message),
        )

    def _persist_snapshot(self, sample: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO server_status_snapshots
            (node_id, collected_at, cpu_percent, load1, load5, load15, memory_percent,
             swap_percent, disk_percent, net_recv_bps, net_sent_bps, disk_read_bps,
             disk_write_bps, connection_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                sample["node_id"],
                sample["collected_at"],
                sample["cpu_percent"],
                sample["load1"],
                sample["load5"],
                sample["load15"],
                sample["memory_percent"],
                sample["swap_percent"],
                sample["disk_percent"],
                sample["net_recv_bps"],
                sample["net_sent_bps"],
                sample["disk_read_bps"],
                sample["disk_write_bps"],
                sample["connection_count"],
            ),
        )

    def _maybe_rollup_and_cleanup(self) -> None:
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        if self._last_rollup_hour == now:
            return
        self._last_rollup_hour = now
        rollup_previous_hour(self.db, now)
        cleanup_server_status(
            self.db,
            raw_retention_days=int(self.config.server_status.raw_retention_days),
            rollup_retention_days=int(self.config.server_status.rollup_retention_days),
        )


def latest_server_status(db: Database) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT node_id, hostname, pid, status, latest_json, last_error,
               heartbeat_at, lease_expires_at
        FROM server_status_collectors
        ORDER BY heartbeat_at DESC
        LIMIT 1
        """
    )
    if not rows:
        return {"status": "empty", "sample": None, "stale": True, "last_error": ""}
    row = rows[0]
    sample = _loads(row.get("latest_json") or "{}")
    heartbeat_at = row.get("heartbeat_at")
    stale = True
    if heartbeat_at:
        stale = heartbeat_at < datetime.now() - timedelta(seconds=30)
    return {
        "status": row.get("status") or "unknown",
        "node_id": row.get("node_id"),
        "hostname": row.get("hostname"),
        "pid": row.get("pid"),
        "heartbeat_at": _dt(heartbeat_at),
        "lease_expires_at": _dt(row.get("lease_expires_at")),
        "stale": stale,
        "last_error": row.get("last_error") or "",
        "sample": sample if sample else None,
    }


def server_status_history(db: Database, range_name: str) -> dict[str, Any]:
    if range_name not in HISTORY_RANGES:
        range_name = "1h"
    duration, bucket_seconds = HISTORY_RANGES[range_name]
    since = datetime.now() - duration
    if range_name == "30d":
        rows = db.query(
            """
            SELECT bucket_start AS collected_at,
                   avg_cpu_percent AS cpu_percent,
                   max_cpu_percent,
                   avg_memory_percent AS memory_percent,
                   max_memory_percent,
                   avg_disk_percent AS disk_percent,
                   max_disk_percent,
                   avg_load1 AS load1,
                   avg_net_recv_bps AS net_recv_bps,
                   avg_net_sent_bps AS net_sent_bps,
                   avg_connection_count AS connection_count
            FROM server_status_rollups_hourly
            WHERE bucket_start >= %s
            ORDER BY bucket_start ASC
            """,
            (since,),
        )
    else:
        rows = db.query(
            """
            SELECT FROM_UNIXTIME(bucket_epoch) AS collected_at,
                   AVG(cpu_percent) AS cpu_percent,
                   MAX(cpu_percent) AS max_cpu_percent,
                   AVG(memory_percent) AS memory_percent,
                   MAX(memory_percent) AS max_memory_percent,
                   AVG(disk_percent) AS disk_percent,
                   MAX(disk_percent) AS max_disk_percent,
                   AVG(load1) AS load1,
                   AVG(net_recv_bps) AS net_recv_bps,
                   AVG(net_sent_bps) AS net_sent_bps,
                   AVG(connection_count) AS connection_count
            FROM (
                SELECT FLOOR(UNIX_TIMESTAMP(collected_at)/%s)*%s AS bucket_epoch,
                       cpu_percent,
                       memory_percent,
                       disk_percent,
                       load1,
                       net_recv_bps,
                       net_sent_bps,
                       connection_count
                FROM server_status_snapshots
                WHERE collected_at >= %s
            ) bucketed
            GROUP BY bucket_epoch
            ORDER BY bucket_epoch ASC
            """,
            (bucket_seconds, bucket_seconds, since),
        )
    return {"range": range_name, "bucket_seconds": bucket_seconds, "items": [_history_row(row) for row in rows]}


def rollup_previous_hour(db: Database, current_hour: datetime | None = None) -> None:
    current_hour = current_hour or datetime.now().replace(minute=0, second=0, microsecond=0)
    previous_hour = current_hour - timedelta(hours=1)
    db.execute(
        """
        INSERT INTO server_status_rollups_hourly
        (node_id, bucket_start, sample_count, avg_cpu_percent, max_cpu_percent,
         avg_memory_percent, max_memory_percent, avg_disk_percent, max_disk_percent,
         avg_load1, avg_net_recv_bps, avg_net_sent_bps, avg_connection_count)
        SELECT node_id, %s, COUNT(*), AVG(cpu_percent), MAX(cpu_percent),
               AVG(memory_percent), MAX(memory_percent), AVG(disk_percent), MAX(disk_percent),
               AVG(load1), AVG(net_recv_bps), AVG(net_sent_bps), AVG(connection_count)
        FROM server_status_snapshots
        WHERE collected_at >= %s AND collected_at < %s
        GROUP BY node_id
        ON DUPLICATE KEY UPDATE
            sample_count=VALUES(sample_count),
            avg_cpu_percent=VALUES(avg_cpu_percent),
            max_cpu_percent=VALUES(max_cpu_percent),
            avg_memory_percent=VALUES(avg_memory_percent),
            max_memory_percent=VALUES(max_memory_percent),
            avg_disk_percent=VALUES(avg_disk_percent),
            max_disk_percent=VALUES(max_disk_percent),
            avg_load1=VALUES(avg_load1),
            avg_net_recv_bps=VALUES(avg_net_recv_bps),
            avg_net_sent_bps=VALUES(avg_net_sent_bps),
            avg_connection_count=VALUES(avg_connection_count)
        """,
        (previous_hour, previous_hour, current_hour),
    )


def cleanup_server_status(db: Database, *, raw_retention_days: int, rollup_retention_days: int) -> None:
    db.execute(
        "DELETE FROM server_status_snapshots WHERE collected_at < %s",
        (datetime.now() - timedelta(days=raw_retention_days),),
    )
    db.execute(
        "DELETE FROM server_status_rollups_hourly WHERE bucket_start < %s",
        (datetime.now() - timedelta(days=rollup_retention_days),),
    )


def _history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "collected_at": _dt(row.get("collected_at")),
        "cpu_percent": _num(row.get("cpu_percent")),
        "max_cpu_percent": _num(row.get("max_cpu_percent")),
        "memory_percent": _num(row.get("memory_percent")),
        "max_memory_percent": _num(row.get("max_memory_percent")),
        "disk_percent": _num(row.get("disk_percent")),
        "max_disk_percent": _num(row.get("max_disk_percent")),
        "load1": _num(row.get("load1")),
        "net_recv_bps": _num(row.get("net_recv_bps")),
        "net_sent_bps": _num(row.get("net_sent_bps")),
        "connection_count": _num(row.get("connection_count")),
    }


def _loadavg() -> tuple[float, float, float]:
    try:
        return tuple(float(value) for value in os.getloadavg())
    except (AttributeError, OSError):
        return 0.0, 0.0, 0.0


def _dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value or "")


def _num(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _loads(value: str) -> dict[str, Any]:
    try:
        import json

        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}
