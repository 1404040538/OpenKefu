from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime

from openkefu.web.config import AppConfig


_RUN_ID = ""
_RUN_STARTED_AT: datetime | None = None
_STOP_LOGGED = False


class RuntimeContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _RUN_ID or "-"
        return True


def configure_logging(config: AppConfig) -> None:
    global _RUN_ID, _RUN_STARTED_AT, _STOP_LOGGED

    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    _RUN_STARTED_AT = datetime.now()
    timestamp = _RUN_STARTED_AT.strftime("%Y%m%d_%H%M%S")
    _RUN_ID = f"{timestamp}_{uuid.uuid4().hex[:8]}"
    _STOP_LOGGED = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | run_id=%(run_id)s | pid=%(process)d | module=%(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = RuntimeContextFilter()
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.addFilter(context_filter)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    logging.getLogger(__name__).info(
        "event=logging.configured storage=database started_at=%s pid=%s",
        _RUN_STARTED_AT.isoformat(sep=" ", timespec="seconds"),
        os.getpid(),
    )


def get_logging_run_info() -> dict[str, str | int]:
    return {
        "run_id": _RUN_ID,
        "started_at": _RUN_STARTED_AT.isoformat(sep=" ", timespec="seconds") if _RUN_STARTED_AT else "",
        "pid": os.getpid(),
    }


def mark_runtime_stopped() -> bool:
    global _STOP_LOGGED
    if _STOP_LOGGED or not _RUN_STARTED_AT:
        return False
    _STOP_LOGGED = True
    return True


def runtime_uptime_seconds() -> int:
    if not _RUN_STARTED_AT:
        return 0
    stopped_at = datetime.now()
    return int((stopped_at - _RUN_STARTED_AT).total_seconds())
