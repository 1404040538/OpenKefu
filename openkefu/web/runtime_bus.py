from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Callable

import redis
from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger(__name__)

EVENT_CHANNEL = "openkefu:events"
COMMAND_CHANNEL = "openkefu:runtime_commands"
REPLY_CHANNEL_PREFIX = "openkefu:runtime_replies:"


def make_redis_client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True, health_check_interval=30)


class RuntimeCommandBus:
    def __init__(self, redis_url: str, command_secret: str):
        self.redis = make_redis_client(redis_url)
        self._secret = command_secret.encode("utf-8")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(self._secret).digest()))
        self._hmac_key = hashlib.sha256(b"pdd-agent-runtime-bus:" + self._secret).digest()
        self._seen_nonces: dict[str, float] = {}

    def call(self, action: str, shop_id: int | None = None, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        command_id = uuid.uuid4().hex
        reply_channel = f"{REPLY_CHANNEL_PREFIX}{command_id}"
        now = time.time()
        payload = {
            "id": command_id,
            "action": action,
            "shop_id": int(shop_id) if shop_id is not None else None,
            "params": params or {},
            "reply_channel": reply_channel,
            "created_at": now,
        }
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(reply_channel)
        try:
            self.redis.publish(COMMAND_CHANNEL, self._encode_message(payload, ttl_seconds=max(timeout, 5.0)))
            deadline = time.time() + timeout
            while time.time() < deadline:
                message = pubsub.get_message(timeout=0.5)
                if not message:
                    continue
                try:
                    data = self._decode_message(message["data"])
                except ValueError:
                    logger.warning("invalid runtime command reply dropped")
                    continue
                if data.get("id") != command_id:
                    continue
                if data.get("ok"):
                    return data.get("result")
                raise RuntimeError(str(data.get("error") or "runtime command failed"))
            raise TimeoutError(f"runtime command timed out: {action}")
        finally:
            try:
                pubsub.unsubscribe(reply_channel)
                pubsub.close()
            except Exception:
                pass

    def reply(self, reply_channel: str, command_id: str, *, ok: bool, result: Any = None, error: str | None = None) -> None:
        self.redis.publish(
            reply_channel,
            self._encode_message({"id": command_id, "ok": ok, "result": result, "error": error}, ttl_seconds=60.0),
        )

    def listen(self, handler: Callable[[dict[str, Any]], bool], *, stop_checker: Callable[[], bool] | None = None) -> None:
        # Redis 断线/抖动时 pubsub.get_message 会抛异常；必须在循环内捕获并
        # 重建订阅，否则 worker 的 command_loop 会异常退出、所有在线店铺断线。
        while not (stop_checker and stop_checker()):
            pubsub = None
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(COMMAND_CHANNEL)
                while not (stop_checker and stop_checker()):
                    try:
                        message = pubsub.get_message(timeout=1.0)
                    except Exception as exc:
                        logger.warning("runtime command redis error, reconnecting in 3s: %s", exc)
                        break
                    if not message:
                        continue
                    try:
                        payload = self._decode_message(message["data"])
                    except ValueError:
                        logger.warning("invalid runtime command payload dropped")
                        continue
                    try:
                        handled = handler(payload)
                        if handled:
                            logger.debug("runtime command handled: %s", payload.get("action"))
                    except Exception:
                        logger.exception("runtime command handler crashed")
            except Exception as exc:
                logger.warning("runtime command listen crashed, reconnecting in 3s: %s", exc)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.unsubscribe(COMMAND_CHANNEL)
                        pubsub.close()
                    except Exception:
                        pass
            if stop_checker and stop_checker():
                return
            time.sleep(3)

    def _encode_message(self, payload: dict[str, Any], *, ttl_seconds: float) -> str:
        now = time.time()
        body = dict(payload)
        body["nonce"] = uuid.uuid4().hex
        body["expires_at"] = now + max(1.0, float(ttl_seconds))
        token = self._fernet.encrypt(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")).decode("ascii")
        signature = hmac.new(self._hmac_key, token.encode("ascii"), hashlib.sha256).hexdigest()
        return json.dumps({"v": 1, "payload": token, "sig": signature}, separators=(",", ":"))

    def _decode_message(self, raw: str) -> dict[str, Any]:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid runtime command envelope") from exc
        if not isinstance(envelope, dict) or envelope.get("v") != 1:
            raise ValueError("unsupported runtime command envelope")
        token = str(envelope.get("payload") or "")
        signature = str(envelope.get("sig") or "")
        expected = hmac.new(self._hmac_key, token.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid runtime command signature")
        try:
            body = json.loads(self._fernet.decrypt(token.encode("ascii")).decode("utf-8"))
        except (InvalidToken, TypeError, ValueError) as exc:
            raise ValueError("invalid runtime command ciphertext") from exc
        if not isinstance(body, dict):
            raise ValueError("invalid runtime command body")
        if float(body.get("expires_at") or 0) < time.time():
            raise ValueError("expired runtime command")
        nonce = str(body.get("nonce") or "")
        if not nonce:
            raise ValueError("runtime command missing nonce")
        self._remember_nonce(nonce, float(body.get("expires_at") or 0))
        body.pop("nonce", None)
        body.pop("expires_at", None)
        return body

    def _remember_nonce(self, nonce: str, expires_at: float) -> None:
        now = time.time()
        if len(self._seen_nonces) > 10000:
            self._seen_nonces = {
                key: value
                for key, value in self._seen_nonces.items()
                if value >= now
            }
        if nonce in self._seen_nonces:
            raise ValueError("replayed runtime command nonce")
        self._seen_nonces[nonce] = expires_at
