import signal
import os
import threading

from openkefu.web.config import load_config
from openkefu.web.db import Database
from openkefu.web.logging_config import configure_logging
from openkefu.web.realtime import RealtimeHub, RuntimeLogger
from openkefu.web.shop_runtime import ShopRuntimeManager


def main():
    os.environ.setdefault("OPENKEFU_RUNTIME_ROLE", "worker")
    config = load_config()
    configure_logging(config)
    db = Database(config)
    db.initialize()
    hub = RealtimeHub(config)
    runtime_logger = RuntimeLogger(db, hub)
    runtime = ShopRuntimeManager(db, hub, runtime_logger, config)

    stop_event = threading.Event()

    def stop(*_):
        stop_event.set()
        runtime.stop_background_services()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"[runtime-worker] worker_id={runtime.worker_id}")
    try:
        runtime.command_loop()
    finally:
        runtime.stop_background_services()


if __name__ == "__main__":
    main()
