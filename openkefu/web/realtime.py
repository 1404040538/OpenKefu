from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from openkefu.web.config import AppConfig
from openkefu.web.db import Database, json_dumps
from openkefu.web.logging_config import get_logging_run_info
from openkefu.web.runtime_bus import EVENT_CHANNEL, make_redis_client


logger = logging.getLogger(__name__)


SENSITIVE_LOG_KEYWORDS = (
    "access_token",
    "authorization",
    "cookie",
    "cookies",
    "crawlerinfo",
    "mobileverifycode",
    "password",
    "pass_id",
    "risk_sign",
    "riskSign",
    "token",
    "verify_auth_token",
    "verifyauthtoken",
    "verificationcode",
)


def sanitize_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.replace("-", "_").lower()
            if any(marker.lower() in normalized for marker in SENSITIVE_LOG_KEYWORDS):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = sanitize_log_value(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_log_value(item) for item in value]
    return value


def sanitize_log_text(value: str | None) -> str | None:
    if not value:
        return value
    text = str(value)
    patterns = (
        r"(?i)(cookie|cookies|authorization|access_token|token|password|verify_auth_token|verificationCode|mobileVerifyCode)(['\"]?\s*[:=]\s*)['\"]?[^'\"\s,}]+",
    )
    for pattern in patterns:
        text = re.sub(pattern, r"\1\2[REDACTED]", text)
    return text


@dataclass(frozen=True)
class RealtimeClient:
    user_id: int
    role: str
    shop_ids: frozenset[int]


class RealtimeHub:
    def __init__(self, config: AppConfig | None = None):
        self._clients: dict[WebSocket, RealtimeClient] = {}
        self._lock = asyncio.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._redis = make_redis_client(config.redis.url) if config and config.runtime.role != "both" else None
        self._subscriber_task: asyncio.Task | None = None

    async def connect(
        self,
        websocket: WebSocket,
        *,
        user_id: int,
        role: str,
        shop_ids: set[int],
        subprotocol: str | None = None,
    ) -> None:
        # Browsers reject the handshake when they request a WebSocket
        # subprotocol and the server does not echo the selected protocol.
        # Authentication is carried in that protocol, so accept it explicitly.
        await websocket.accept(subprotocol=subprotocol)
        client = RealtimeClient(
            user_id=int(user_id),
            role=str(role or ""),
            shop_ids=frozenset(int(shop_id) for shop_id in shop_ids),
        )
        async with self._lock:
            self._clients[websocket] = client

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)

    async def _disconnect_user(self, user_id: int, code: int = 4001) -> None:
        async with self._lock:
            sockets = [
                websocket
                for websocket, client in self._clients.items()
                if client.user_id == int(user_id)
            ]
            for websocket in sockets:
                self._clients.pop(websocket, None)
        for websocket in sockets:
            try:
                await websocket.close(code=code)
            except Exception:
                pass

    def disconnect_user(self, user_id: int) -> None:
        """Expire active realtime sessions after an account security change."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._disconnect_user(user_id), self.loop)

    async def broadcast(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        payload = json.dumps(event, ensure_ascii=False, default=str)
        async with self._lock:
            clients = list(self._clients.items())
        dead = []
        for websocket, client in clients:
            if not self._can_receive(client, event):
                continue
            try:
                await websocket.send_text(payload)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.pop(client, None)

    @staticmethod
    def _can_receive(client: RealtimeClient, event: dict[str, Any]) -> bool:
        if client.role == "admin":
            return True

        shop_ids = _event_shop_ids(event)
        if shop_ids and not shop_ids.issubset(client.shop_ids):
            return False

        user_ids = _event_user_ids(event)
        if user_ids and client.user_id not in user_ids:
            return False

        return bool(shop_ids or user_ids)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self._redis and self._subscriber_task is None:
            self._subscriber_task = asyncio.create_task(self._redis_subscriber())

    async def stop(self) -> None:
        task = self._subscriber_task
        if task:
            task.cancel()
            self._subscriber_task = None

    async def _redis_subscriber(self) -> None:
        # Redis 断线时 get_message 抛异常；必须捕获并重建订阅，
        # 否则实时推送静默失效（前端收不到任何事件），需重启 API 才能恢复。
        while True:
            pubsub = None
            try:
                pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(EVENT_CHANNEL)
                while True:
                    try:
                        message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("realtime redis subscriber error, reconnecting in 3s: %s", exc)
                        break
                    if not message:
                        continue
                    try:
                        event = json.loads(message["data"])
                    except Exception:
                        logger.exception("invalid realtime event payload")
                        continue
                    await self.broadcast(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("realtime redis subscriber crashed, reconnecting in 3s: %s", exc)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.unsubscribe(EVENT_CHANNEL)
                        pubsub.close()
                    except Exception:
                        pass
            await asyncio.sleep(3)

    def publish(self, event: dict[str, Any]) -> None:
        if self._redis:
            event = dict(event)
            event.setdefault("ts", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            try:
                self._redis.publish(EVENT_CHANNEL, json.dumps(event, ensure_ascii=False, default=str))
                return
            except Exception:
                logger.exception("failed to publish realtime event to redis")
        if not self.loop:
            logger.debug("realtime hub has no loop yet: %s", event)
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(event), self.loop)


def _event_containers(event: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [event]
    data = event.get("data")
    if isinstance(data, dict):
        containers.append(data)
    return containers


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_shop_ids(event: dict[str, Any]) -> frozenset[int]:
    shop_ids: set[int] = set()
    for container in _event_containers(event):
        shop_id = _coerce_int(container.get("shop_id"))
        if shop_id is not None:
            shop_ids.add(shop_id)
        raw_shop_ids = container.get("shop_ids")
        if isinstance(raw_shop_ids, (list, tuple, set)):
            for raw_shop_id in raw_shop_ids:
                shop_id = _coerce_int(raw_shop_id)
                if shop_id is not None:
                    shop_ids.add(shop_id)
    return frozenset(shop_ids)


def _event_user_ids(event: dict[str, Any]) -> frozenset[int]:
    user_ids: set[int] = set()
    for container in _event_containers(event):
        user_id = _coerce_int(container.get("user_id"))
        if user_id is not None:
            user_ids.add(user_id)
    return frozenset(user_ids)


class RuntimeLogger:
    def __init__(self, db: Database, hub: RealtimeHub):
        self.db = db
        self.hub = hub
        self._queue: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=5000)
        self._worker = threading.Thread(target=self._flush_loop, name="runtime-log-writer", daemon=True)
        self._worker.start()

    def _flush_loop(self) -> None:
        batch: list[tuple[Any, ...]] = []
        sql = """
            INSERT INTO runtime_logs
            (level, module, action, message, shop_id, mall_id, conversation_id,
             user_uid, request_id, run_id, pid, error_trace, context_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        while True:
            try:
                item = self._queue.get(timeout=0.5)
                batch.append(item)
                while len(batch) < 100:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if not batch:
                continue
            try:
                self.db.execute_many(sql, batch)
            except Exception:
                logger.exception("failed to write runtime log batch")
            batch.clear()

    def log(
        self,
        level: str,
        module: str,
        action: str,
        message: str,
        *,
        shop_id=None,
        mall_id=None,
        conversation_id=None,
        user_uid=None,
        request_id=None,
        error: Exception | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        level = level.upper()
        safe_context = sanitize_log_value(context or {})
        error_trace = sanitize_log_text("".join(traceback.format_exception(error))) if error else None
        run_info = get_logging_run_info()
        run_id = str(run_info.get("run_id") or "")
        pid = int(run_info.get("pid") or os.getpid())
        logging.getLogger(module).log(
            getattr(logging, level, logging.INFO),
            "event=%s shop_id=%s mall_id=%s conversation_id=%s user_uid=%s request_id=%s %s",
            action,
            shop_id,
            mall_id,
            conversation_id,
            user_uid,
            request_id,
            message,
        )
        try:
            self._queue.put_nowait(
                (
                    level,
                    module,
                    action,
                    message,
                    shop_id,
                    mall_id,
                    conversation_id,
                    user_uid,
                    request_id,
                    run_id,
                    pid,
                    error_trace,
                    json_dumps({**safe_context, "run_id": run_id, "pid": pid}),
                )
            )
        except queue.Full:
            logger.warning("runtime log queue is full; dropping log action=%s", action)
        log_id = None
        self.hub.publish(
            {
                "type": "runtime_log",
                "data": {
                    "id": log_id,
                    "level": level,
                    "module": module,
                    "action": action,
                    "message": message,
                    "shop_id": shop_id,
                    "mall_id": mall_id,
                    "conversation_id": conversation_id,
                    "user_uid": user_uid,
                    "request_id": request_id,
                    "run_id": run_id,
                    "pid": pid,
                    "error_trace": error_trace,
                    "context": safe_context,
                },
            }
        )
