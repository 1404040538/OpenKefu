from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import qrcode as qrcode_lib
from pymysql.err import IntegrityError

from openkefu.platforms.pdd.auth.login import CaptchaRequiredError, Login
from openkefu.platforms.pdd.chat.auto_reply import (
    incoming_chat_messages,
    incoming_display_messages,
    is_system_notice_message,
    message_dedupe_key,
    user_message_summary,
)
from openkefu.platforms.pdd.chat.actions import ActionDispatcher
from openkefu.platforms.pdd.chat.conversation import build_conversation_messages
from openkefu.platforms.pdd.chat.customer_service import CustomerServiceClient
from openkefu.platforms.pdd.chat.customer_transfer import CustomerTransferClient
from openkefu.platforms.pdd.chat.fast_reply import detect_fast_reply
from openkefu.platforms.pdd.chat.goods import GoodsService
from openkefu.platforms.pdd.chat.intent import clean_reply_text
from openkefu.platforms.pdd.chat.knowledge import KnowledgeService, NoteSetService
from openkefu.platforms.pdd.chat.llm import LLMClient, get_llm_client
from openkefu.platforms.pdd.chat.llm_reply import analyze_customer_intent
from openkefu.platforms.pdd.chat.orders import OrderService
from openkefu.platforms.pdd.chat.reply_cache import ReplyCache
from openkefu.platforms.pdd.chat.return_records import ReturnRecordService
from openkefu.platforms.pdd.chat.send_policy import (
    is_session_expired,
    send_message_failure_text,
    send_message_response_ok,
)
from openkefu.platforms.pdd.config import QRCODE_DIR
from openkefu.web.config import AppConfig
from openkefu.web.crypto import TextCipher
from openkefu.web.db import Database, json_dumps
from openkefu.web.ratelimit import FixedWindowRateLimiter
from openkefu.web.realtime import RealtimeHub, RuntimeLogger
from openkefu.web.runtime_bus import RuntimeCommandBus


class PasswordVerificationRequired(RuntimeError):
    """Signal an interactive password-login challenge without losing its payload."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(str(payload.get("message") or "登录需要完成验证"))


class ShopRunner:
    PASSWORD_VERIFY_TTL_SECONDS = 300
    PASSWORD_VERIFY_MAX_ATTEMPTS = 5
    MAX_REPLIED_KEYS = 20000

    def __init__(self, shop_id: int, db: Database, hub: RealtimeHub, runtime_logger: RuntimeLogger, config: AppConfig,
                 llm_client: LLMClient | None = None, reply_cache: ReplyCache | None = None, manager=None, note_service: NoteSetService | None = None):
        self.shop_id = int(shop_id)
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.config = config
        self._secret_cipher = TextCipher(config.security.data_encryption_key)
        self.llm_client = llm_client or get_llm_client(config.llm)
        self.knowledge = KnowledgeService(db, config)
        self.note_service = note_service or NoteSetService(db)
        self.actions = ActionDispatcher(db, hub, runtime_logger)
        self.return_records = ReturnRecordService(db, hub, runtime_logger)
        self.reply_cache = reply_cache
        self.manager = manager
        self.lock = threading.RLock()
        self.login: Login | None = None
        self.customer_service: CustomerServiceClient | None = None
        self.transfer_client: CustomerTransferClient | None = None
        self.goods_service: GoodsService | None = None
        self.order_service: OrderService | None = None
        self.listener: dict[str, Any] | None = None
        self.session_id: int | None = None
        self.token_result: dict[str, Any] | None = None
        self.mall_id: str | None = None
        self.replied_keys: set[tuple[Any, ...]] = set()
        self.reply_state_lock = threading.RLock()
        self.pending_reply_messages: dict[int, list[tuple[dict[str, Any], int]]] = {}
        self.reply_workers: dict[int, Any] = {}
        self.reply_batch_delay = 0.5
        self.latest_product_context: dict[int, dict[str, Any]] = {}
        self.thread: threading.Thread | None = None
        self.latest_conversations_sync_index = 0
        self._password_login_state: dict[str, Any] | None = None
        # WS 断线自动重连状态（指数退避）
        self._ws_reconnect_attempts = 0
        self._ws_reconnect_timer: threading.Timer | None = None
        self._ws_reconnect_generation = 0
        self.MAX_WS_RECONNECT_ATTEMPTS = 8
        self.WS_RECONNECT_BASE_DELAY = 5.0
        self.WS_RECONNECT_MAX_DELAY = 120.0
        # 自动续登状态（登录态过期时用保存的账密自动重新登录）
        self._last_auto_relogin_at = 0.0
        self.AUTO_RELOGIN_MIN_INTERVAL = 600.0

    def start(self) -> dict[str, Any]:
        return self.login_shop()

    def login_shop(self) -> dict[str, Any]:
        with self.lock:
            self._require_creator_llm_quota("大模型调用额度不足，无法登入店铺")
            self._disconnect_customer_service(update_session=True)
            with self.reply_state_lock:
                self.pending_reply_messages.clear()
                self.reply_workers.clear()
                self.replied_keys.clear()
            self._set_shop_status("login_pending")
            self.thread = threading.Thread(target=self._run_login_flow, name=f"shop-{self.shop_id}-runner", daemon=True)
            self.thread.start()
            return {"status": "login_pending", "qrcode_url": None}

    def password_login_shop(self, username: str, password: str, verify_code: str = "") -> dict[str, Any]:
        with self.lock:
            if verify_code:
                return self._complete_password_login(verify_code)

            self._require_creator_llm_quota("大模型调用额度不足，无法登入店铺")
            self._disconnect_customer_service(update_session=True)
            with self.reply_state_lock:
                self.pending_reply_messages.clear()
                self.reply_workers.clear()
                self.replied_keys.clear()

            # 第一步：同步执行，立即返回结果给前端
            self.login = self._new_login_with_cached_context()
            login_result = self.login.do_password_login(username, password)

            if isinstance(login_result, dict) and login_result.get("need_verify"):
                return self._begin_password_verification(
                    self.login,
                    login_result,
                    username=username,
                    password=password,
                    log_action="shop.password_login.verify_required",
                )

            # 登录成功，后续补齐 cookies 和上线在后台线程执行
            self._password_login_state = None
            self._set_shop_status("login_pending")
            self.thread = threading.Thread(
                target=self._finish_password_login,
                args=(login_result,),
                name=f"shop-{self.shop_id}-pwd-finish",
                daemon=True,
            )
            self.thread.start()
            return {"status": "login_success", "qrcode_url": None}

    def online(self) -> dict[str, Any]:
        with self.lock:
            self._require_creator_llm_quota("大模型调用额度不足，无法上线店铺")
            if isinstance(self.listener, dict) and self.listener.get("client"):
                self._start_latest_conversations_sync(reason="already_online")
                return {"status": "online", "shop": self._shop_row()}
            if isinstance(self.listener, dict) and self.listener.get("thread") and self.listener["thread"].is_alive():
                return {"status": "connecting", "shop": self._shop_row()}
            if self.listener is not None:
                return {"status": "connecting", "shop": self._shop_row()}
            self._set_shop_status("connecting")
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            try:
                self._connect_customer_service_from_cache()
            except PasswordVerificationRequired as exc:
                return {**exc.payload, "shop": self._shop_row()}
            except Exception as exc:
                self._set_error(exc)
                raise
            return {"status": "online", "shop": self._shop_row()}

    def offline(self) -> None:
        with self.lock:
            self._disconnect_customer_service(update_session=True)
            self._set_shop_status("offline")
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def stop(self) -> None:
        with self.lock:
            self._disconnect_customer_service(update_session=True, session_status="stopped")
            self._set_shop_status("stopped")
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def _disconnect_customer_service(self, *, update_session: bool, session_status: str = "offline", cancel_reconnect: bool = True) -> None:
        self._password_login_state = None
        if cancel_reconnect:
            self._cancel_ws_reconnect()
        if isinstance(self.listener, dict) and self.listener.get("client"):
            try:
                self.listener["client"].close()
            except Exception as exc:
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "shop.disconnect.close_client",
                    "failed to close titan client",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    error=exc,
                )
        if update_session and self.session_id:
            self.db.execute(
                "UPDATE shop_sessions SET status=%s, ended_at=NOW() WHERE id=%s",
                (session_status, self.session_id),
            )
        self.listener = None
        self.customer_service = None
        self.transfer_client = None
        self.goods_service = None
        self.order_service = None
        self.token_result = None
        self.session_id = None

    def latest_qrcode(self) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM qr_login_attempts WHERE shop_id=%s ORDER BY id DESC LIMIT 1",
            (self.shop_id,),
        )
        if not row:
            return None
        return f"/api/shops/{self.shop_id}/qrcode?attempt_id={row['id']}"

    def _run_login_flow(self) -> None:
        try:
            self.login = self._new_login_with_cached_context()
            qrcode_info = self.login.get_qrcode()
            
            if not qrcode_info or not qrcode_info.get("token"):
                raise RuntimeError("failed to get valid qrcode info")
            
            qrcode_path = self._save_qrcode(qrcode_info["uri"])
            self.session_id = self.db.execute(
                "INSERT INTO shop_sessions (shop_id, status) VALUES (%s, 'qr_pending')",
                (self.shop_id,),
            )
            attempt_id = self.db.execute(
                """
                INSERT INTO qr_login_attempts (shop_id, session_id, token, qrcode_path, status)
                VALUES (%s,%s,%s,%s,'pending')
                """,
                (self.shop_id, self.session_id, qrcode_info["token"], str(qrcode_path)),
            )
            self._set_shop_status("qr_pending")
            # 状态变化必须推送 shop_status，否则前端卡片停留在旧状态（不自动更新）
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.hub.publish(
                {
                    "type": "qrcode",
                    "data": {
                        "shop_id": self.shop_id,
                        "attempt_id": attempt_id,
                        "qrcode_url": f"/api/shops/{self.shop_id}/qrcode?attempt_id={attempt_id}",
                    },
                }
            )
            self.runtime_logger.log("INFO", __name__, "shop.qrcode", "login qrcode generated", shop_id=self.shop_id)

            last_login_progress_status = "qr_pending"

            def on_login_status(query_result: dict[str, Any]) -> None:
                nonlocal last_login_progress_status
                qr_status = query_result.get("status")
                progress_status = None
                if qr_status == 2:
                    progress_status = "qr_scanned"
                elif qr_status == 3 or query_result.get("isSuccess"):
                    progress_status = "login_success"
                if not progress_status or progress_status == last_login_progress_status:
                    return
                last_login_progress_status = progress_status
                self._set_login_progress(progress_status, attempt_id, query_result)

            try:
                login_result = self.login.wait_for_login_and_get_target_cookies(
                    qrcode_info["token"],
                    on_status=on_login_status,
                )
            except Exception as poll_exc:
                self.db.execute(
                    "UPDATE qr_login_attempts SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                    (str(poll_exc), attempt_id),
                )
                raise

            identity = CustomerServiceClient._pass_id_identity(login_result.get("cookies") or {})
            new_mall_id = str(identity.get("mall_id") or self.mall_id or "")
            if new_mall_id:
                self._assert_shop_identity(new_mall_id, source="qr_login")
                self.mall_id = new_mall_id
            with self.db.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE qr_login_attempts SET status='success', query_result=%s, completed_at=NOW() WHERE id=%s",
                        (json_dumps(login_result.get("query") or {}), attempt_id),
                    )
                    self._store_login_cache_cursor(cursor, login_result)
                    cursor.execute(
                        """
                        UPDATE shops SET status='logged_in', mall_id=%s, last_error=NULL WHERE id=%s
                        """,
                        (self.mall_id, self.shop_id),
                    )
                    cursor.execute(
                        """
                        UPDATE shop_sessions
                        SET status='logged_in', mall_id=%s
                        WHERE id=%s
                        """,
                        (self.mall_id, self.session_id),
                    )
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.runtime_logger.log(
                "INFO",
                __name__,
                "shop.login",
                "shop login cache saved",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
            )
            self._connect_customer_service_from_cache()
        except CaptchaRequiredError as exc:
            # 扫码登录遇到图形验证码：给出明确提示，不再误报"登录失败/重试"
            self._set_error(
                RuntimeError("扫码登录需要图形验证码，请稍后重试或改用账密登录")
            )
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "shop.qrcode.captcha_required",
                "qrcode login requires captcha",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )
        except Exception as exc:
            self._set_error(exc)

    def _finish_password_login(self, login_result: dict) -> None:
        try:
            identity = CustomerServiceClient._pass_id_identity(login_result.get("cookies") or {})
            new_mall_id = str(identity.get("mall_id") or self.mall_id or "")
            if new_mall_id:
                self._assert_shop_identity(new_mall_id, source="password_login")
                self.mall_id = new_mall_id
            with self.db.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO shop_sessions (shop_id, status) VALUES (%s, 'login_success')",
                        (self.shop_id,),
                    )
                    self.session_id = cursor.lastrowid
                    self._store_login_cache_cursor(cursor, login_result)
                    cursor.execute(
                        "UPDATE shops SET status='logged_in', mall_id=%s, last_error=NULL WHERE id=%s",
                        (self.mall_id, self.shop_id),
                    )
                    cursor.execute(
                        "UPDATE shop_sessions SET status='logged_in', mall_id=%s WHERE id=%s",
                        (self.mall_id, self.session_id),
                    )
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.runtime_logger.log(
                "INFO", __name__, "shop.password_login",
                "password login success", shop_id=self.shop_id, mall_id=self.mall_id,
            )
            self._connect_customer_service_from_cache()
        except Exception as exc:
            self._set_error(exc)

    def _complete_password_login(self, verify_code: str) -> dict:
        state = getattr(self, "_password_login_state", None) or {}
        if not state:
            raise RuntimeError("没有待验证的登录会话")

        # 同步执行验证，立即返回结果给前端
        if int(state.get("shop_id") or 0) != self.shop_id:
            self._password_login_state = None
            raise RuntimeError("password login verification state does not match current shop")
        if float(state.get("expires_at") or 0) < time.time():
            self._password_login_state = None
            self._set_error(RuntimeError("password login verification expired"))
            raise RuntimeError("password login verification expired")
        attempts = int(state.get("attempts") or 0) + 1
        state["attempts"] = attempts
        if attempts > self.PASSWORD_VERIFY_MAX_ATTEMPTS:
            self._password_login_state = None
            self._set_error(RuntimeError("password login verification failed too many times"))
            raise RuntimeError("password login verification failed too many times")

        login = state.get("login") or self.login
        if login is None:
            self._password_login_state = None
            raise RuntimeError("登录验证会话已失效，请重新登录")
        result = login.do_password_login(
            state["username"],
            state["password"],
            verify_code=verify_code,
        )

        if isinstance(result, dict) and result.get("need_verify"):
            if str(result.get("verify_type") or "") != "mobile":
                self._password_login_state = None
                message = "password login requires captcha verification; manual captcha flow is not supported"
                self._set_error(RuntimeError(message))
                raise RuntimeError(message)
            return result

        # 验证成功，后台线程完成后续步骤
        self._password_login_state = None
        self.thread = threading.Thread(
            target=self._finish_password_login,
            args=(result,),
            name=f"shop-{self.shop_id}-pwd-finish",
            daemon=True,
        )
        self.thread.start()
        return {"status": "login_success"}

    def _begin_password_verification(
        self,
        login: Login,
        login_result: dict[str, Any],
        *,
        username: str,
        password: str,
        log_action: str,
    ) -> dict[str, Any]:
        verify_type = str(login_result.get("verify_type") or "")
        if verify_type != "mobile":
            self._password_login_state = None
            message = "password login requires captcha verification; manual captcha flow is not supported"
            self.db.execute(
                "UPDATE shops SET status='error', last_error=%s WHERE id=%s",
                (message, self.shop_id),
            )
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            raise RuntimeError(message)

        payload = {
            "status": "verification_required",
            "need_verify": True,
            "verify_type": "mobile",
            "mask_mobile": str(login_result.get("mask_mobile") or ""),
            "message": str(login_result.get("message") or "请输入短信验证码后继续登录"),
        }
        self.login = login
        self._password_login_state = {
            "shop_id": self.shop_id,
            "username": username,
            "password": password,
            "login": login,
            "verify_type": verify_type,
            "mask_mobile": payload["mask_mobile"],
            "expires_at": time.time() + self.PASSWORD_VERIFY_TTL_SECONDS,
            "attempts": 0,
        }
        self.db.execute(
            "UPDATE shops SET status='login_pending', last_error=NULL WHERE id=%s",
            (self.shop_id,),
        )
        self.runtime_logger.log(
            "WARNING",
            __name__,
            log_action,
            "password login requires verification",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            context={
                "verify_type": verify_type,
                "error_code": login_result.get("error_code"),
                "mask_mobile": payload["mask_mobile"],
                "message": payload["message"],
            },
        )
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})
        return payload

    def _set_login_progress(self, status: str, attempt_id: int, query_result: dict[str, Any] | None = None) -> None:
        query_json = json_dumps(query_result or {})
        self.db.execute(
            "UPDATE qr_login_attempts SET status=%s, query_result=%s WHERE id=%s",
            (status, query_json, attempt_id),
        )
        if self.session_id:
            self.db.execute(
                "UPDATE shop_sessions SET status=%s WHERE id=%s",
                (status, self.session_id),
            )
        self._set_shop_status(status)
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def _store_login_cache(self, login_result: dict[str, Any]) -> None:
        self.db.execute(
            self._login_cache_upsert_sql(),
            self._login_cache_values(login_result),
        )

    @staticmethod
    def _login_cache_upsert_sql() -> str:
        return """
            INSERT INTO shop_login_caches
            (shop_id, cookies_json, cookie_string, requests_headers_json, base_headers_json, login_result_json)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                cookies_json=VALUES(cookies_json),
                cookie_string=VALUES(cookie_string),
                requests_headers_json=VALUES(requests_headers_json),
                base_headers_json=VALUES(base_headers_json),
                login_result_json=VALUES(login_result_json)
            """

    def _login_cache_values(self, login_result: dict[str, Any]) -> tuple[Any, ...]:
        return (
            self.shop_id,
            self._encrypt_secret_text(json_dumps(login_result.get("cookies") or {})),
            self._encrypt_secret_text(login_result.get("cookie_string") or ""),
            self._encrypt_secret_text(json_dumps(login_result.get("requests_headers") or {})),
            self._encrypt_secret_text(json_dumps(login_result.get("base_headers") or {})),
            self._encrypt_secret_text(json_dumps(login_result)),
        )

    def _store_login_cache_cursor(self, cursor: Any, login_result: dict[str, Any]) -> None:
        cursor.execute(
            self._login_cache_upsert_sql(),
            self._login_cache_values(login_result),
        )

    def _encrypt_secret_text(self, value: str | None) -> str | None:
        return self._secret_cipher.encrypt(value)

    def _decrypt_secret_text(self, value: str | None) -> str:
        return self._secret_cipher.decrypt(value)

    def _load_secret_json(self, value: str | None, default: Any) -> Any:
        try:
            text = self._decrypt_secret_text(value)
            return json.loads(text) if text else default
        except (TypeError, ValueError):
            return default

    def _persist_current_login_cache(self) -> None:
        with self.lock:
            self._persist_current_login_cache_locked()

    def _persist_current_login_cache_locked(self) -> None:
        if not self.login:
            return
        try:
            cookies = self.login._session_cookies()
        except Exception:
            cookies = getattr(self.login, "cookies", {}) or {}
        if not cookies:
            return
        cookie_string = "; ".join(f"{key}={value}" for key, value in cookies.items() if value)
        row = self.db.query_one("SELECT login_result_json FROM shop_login_caches WHERE shop_id=%s", (self.shop_id,))
        login_result = self._load_secret_json((row or {}).get("login_result_json"), {})
        if not isinstance(login_result, dict):
            login_result = {}
        login_result["cookies"] = cookies
        login_result["cookie_string"] = cookie_string
        login_result["base_headers"] = getattr(self.login, "base_headers", {}) or {}
        login_result["fingerprint_env"] = getattr(self.login, "fingerprint_env", {}) or {}
        self.db.execute(
            """
            UPDATE shop_login_caches
            SET cookies_json=%s,
                cookie_string=%s,
                base_headers_json=%s,
                login_result_json=%s
            WHERE shop_id=%s
            """,
            (
                self._encrypt_secret_text(json_dumps(cookies)),
                self._encrypt_secret_text(cookie_string),
                self._encrypt_secret_text(json_dumps(getattr(self.login, "base_headers", {}) or {})),
                self._encrypt_secret_text(json_dumps(login_result)),
                self.shop_id,
            ),
        )

    def _login_from_cache(self) -> Login:
        row = self.db.query_one("SELECT * FROM shop_login_caches WHERE shop_id=%s", (self.shop_id,))
        if not row:
            raise RuntimeError("店铺还没有登录缓存，请先扫码登入")
        cookies = self._load_secret_json(row.get("cookies_json"), {})
        base_headers = self._load_secret_json(row.get("base_headers_json"), {})
        login = Login()
        login.cookies = cookies
        login.base_headers = base_headers
        for name, value in cookies.items():
            if value:
                login.session.cookies.set(name, value, domain=".pinduoduo.com")
        self._restore_login_context(login, row)
        return login

    def _restore_login_context(self, login: Login, row: dict[str, Any]) -> None:
        login_result = self._load_secret_json(row.get("login_result_json"), {})
        if not isinstance(login_result, dict):
            login_result = {}

        fingerprint_env = login_result.get("fingerprint_env")
        if isinstance(fingerprint_env, dict) and fingerprint_env:
            login.fingerprint_env = fingerprint_env
            return

        base_headers = self._load_secret_json(row.get("base_headers_json"), {})
        if not isinstance(base_headers, dict):
            return
        user_agent = base_headers.get("user-agent") or base_headers.get("User-Agent")
        if not user_agent:
            return
        env = dict(login.fingerprint_env or {})
        navigator = dict(env.get("navigator") or {})
        navigator["ua"] = user_agent
        env["navigator"] = navigator
        login.fingerprint_env = env

    def _new_login_with_cached_context(self) -> Login:
        row = self.db.query_one("SELECT * FROM shop_login_caches WHERE shop_id=%s", (self.shop_id,))
        if not row:
            return Login()
        cookies = self._load_secret_json(row.get("cookies_json"), {})
        base_headers = self._load_secret_json(row.get("base_headers_json"), {})
        login = Login()
        if isinstance(cookies, dict):
            login.cookies = cookies
            for name, value in cookies.items():
                if value:
                    login.session.cookies.set(name, value, domain=".pinduoduo.com")
        if isinstance(base_headers, dict):
            login.base_headers = base_headers
        self._restore_login_context(login, row)
        return login

    @staticmethod
    def _normalize_token_result(token_result: Any) -> dict[str, Any]:
        if not isinstance(token_result, dict):
            raise RuntimeError(f"getToken returned invalid response type: {type(token_result).__name__}")
        nested = token_result.get("result")
        if isinstance(nested, dict):
            merged = dict(token_result)
            merged.update(nested)
            return merged
        return token_result

    @classmethod
    def _access_token_from_token_result(cls, token_result: Any) -> str:
        result = cls._normalize_token_result(token_result)
        for key in ("token", "access_token", "accessToken"):
            value = result.get(key)
            if value:
                return str(value)
        error_code = result.get("error_code") or result.get("errorCode") or result.get("code")
        error_message = (
            result.get("error_msg")
            or result.get("errorMsg")
            or result.get("message")
            or result.get("msg")
            or result.get("reason")
        )
        detail_parts = []
        if error_code is not None:
            detail_parts.append(f"code={error_code}")
        if error_message:
            detail_parts.append(f"message={error_message}")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
        raise RuntimeError(f"getToken response missing access token{detail}")

    def _connect_customer_service_from_cache(self) -> None:
        with self.lock:
            if isinstance(self.listener, dict):
                if self.listener.get("client"):
                    return
                if self.listener.get("thread") and self.listener["thread"].is_alive():
                    return
            if self.listener is not None:
                return
            self.listener = object()
            self._set_shop_status("connecting")
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
        try:
            self.login = self._login_from_cache()
            self.customer_service = CustomerServiceClient(self.login)
            self.transfer_client = CustomerTransferClient(self.customer_service)
            self.goods_service = GoodsService(self.login)
            self.order_service = OrderService(self.login)
            self.session_id = self.db.execute(
                "INSERT INTO shop_sessions (shop_id, status) VALUES (%s, 'connecting')",
                (self.shop_id,),
            )
            version = self.customer_service.get_version()
            self.token_result = self._normalize_token_result(self.customer_service.get_token())
            new_mall_id = str(self.token_result.get("mall_id") or self.mall_id or "")
            if new_mall_id:
                self._assert_shop_identity(new_mall_id, source="connect_from_cache")
                self.mall_id = new_mall_id
            self_nickname = str(self.token_result.get("nickname") or "")
            access_token = self._access_token_from_token_result(self.token_result)
            ws_base_url = self.token_result.get("use_ip")
            self.db.execute(
                "UPDATE shops SET status='online', mall_id=%s, nickname=%s, last_error=NULL WHERE id=%s",
                (self.mall_id, self_nickname if self_nickname else None, self.shop_id),
            )
            self.db.execute(
                """
                UPDATE shop_sessions
                SET status='online', mall_id=%s, access_token=%s, ws_base_url=%s, token_result=%s
                WHERE id=%s
                """,
                (
                    self.mall_id,
                    self._encrypt_secret_text(access_token),
                    ws_base_url,
                    self._encrypt_secret_text(json_dumps(self.token_result)),
                    self.session_id,
                ),
            )
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.runtime_logger.log(
                "INFO",
                __name__,
                "shop.online",
                f"shop websocket starting, version={version}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
            )
            self.listener = self.customer_service.start_titan_listener(
                access_token=access_token,
                token_result=self.token_result,
                on_receive=self._on_titan_receive,
                on_error=self._on_titan_error,
                on_pfb_error=self._on_pfb_error,
                daemon=True,
            )
            with self.lock:
                self._ws_reconnect_attempts = 0
            self._start_latest_conversations_sync(reason="connected")
        except Exception as exc:
            failed_session_id = self.session_id
            self.listener = None
            if failed_session_id:
                self.db.execute(
                    "UPDATE shop_sessions SET status='error', ended_at=NOW(), error=COALESCE(error, 'listener startup failed') WHERE id=%s",
                    (failed_session_id,),
                )
            # 登录态过期（getToken 43001 会话已过期）时，尝试用保存的账密自动重新登录，
            # 成功后继续连接流程，避免每次都要人工重新扫码。
            if self._send_session_expired(exc):
                if self._try_auto_relogin():
                    self._connect_customer_service_from_cache()
                    return
                challenge = self._pending_password_verification()
                if challenge:
                    raise PasswordVerificationRequired(challenge) from exc
            raise

    def _saved_credentials(self) -> dict[str, str] | None:
        row = self.db.query_one(
            "SELECT login_username, login_password FROM shops WHERE id=%s",
            (self.shop_id,),
        )
        if not row:
            return None
        try:
            username = self._decrypt_secret_text(row.get("login_username") or "")
            password = self._decrypt_secret_text(row.get("login_password") or "")
        except (TypeError, ValueError):
            return None
        if not username or not password:
            return None
        return {"username": username, "password": password}

    def _try_auto_relogin(self) -> bool:
        """登录态过期（getToken 43001 等）时，用保存的账密自动重新登录并刷新登录缓存。

        返回 True 表示重登成功（调用方应继续连接流程）；
        无账密/需要验证码/重登失败/频率受限时返回 False，保持原有错误提示。
        """
        if self._pending_password_verification():
            return False
        now = time.time()
        if now - self._last_auto_relogin_at < self.AUTO_RELOGIN_MIN_INTERVAL:
            return False
        credentials = self._saved_credentials()
        if not credentials:
            return False
        self._last_auto_relogin_at = now
        self.runtime_logger.log(
            "INFO",
            __name__,
            "shop.auto_relogin.start",
            "login session expired, auto relogin with saved credentials",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
        )
        try:
            login = Login()
            result = login.do_password_login(credentials["username"], credentials["password"])
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "shop.auto_relogin.failed",
                f"auto relogin failed: {exc}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )
            return False
        if isinstance(result, dict) and result.get("need_verify"):
            self._begin_password_verification(
                login,
                result,
                username=credentials["username"],
                password=credentials["password"],
                log_action="shop.auto_relogin.verify_required",
            )
            return False
        try:
            with self.db.connect() as conn:
                with conn.cursor() as cursor:
                    self._store_login_cache_cursor(cursor, result)
        except Exception as exc:
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "shop.auto_relogin.cache_failed",
                f"auto relogin succeeded but cache save failed: {exc}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )
            return False
        self.runtime_logger.log(
            "INFO",
            __name__,
            "shop.auto_relogin.success",
            "auto relogin success, login cache refreshed",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
        )
        return True

    def _pending_password_verification(self) -> dict[str, Any] | None:
        state = self._password_login_state or {}
        if not state:
            return None
        if float(state.get("expires_at") or 0) < time.time():
            self._password_login_state = None
            return None
        return {
            "status": "verification_required",
            "need_verify": True,
            "verify_type": str(state.get("verify_type") or "mobile"),
            "mask_mobile": str(state.get("mask_mobile") or ""),
            "message": "请输入短信验证码后继续登录",
        }

    def _reconnect_customer_service_from_cache(self, *, reason: str, cancel_reconnect: bool = True) -> None:
        with self.lock:
            self._reconnect_customer_service_locked(reason=reason, cancel_reconnect=cancel_reconnect)

    def _reconnect_customer_service_locked(self, *, reason: str, cancel_reconnect: bool) -> None:
        self.runtime_logger.log(
            "WARNING",
            __name__,
            "shop.reconnect",
            f"reconnecting shop customer service: {reason}",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
        )
        self._persist_current_login_cache()
        self._disconnect_customer_service(
            update_session=True,
            session_status="reconnecting",
            cancel_reconnect=cancel_reconnect,
        )
        self._connect_customer_service_from_cache()

    def _send_session_snapshot(self):
        with self.lock:
            if not self.customer_service or not self.token_result:
                raise RuntimeError("shop is not online")
            return self.customer_service, self.token_result, self.mall_id

    def _recover_send_session(self, failed_client: CustomerServiceClient) -> None:
        # Concurrent replies rejected by the same expired client share one recovery.
        # The lifecycle lock also prevents a manual offline/login from being mixed
        # with a send-triggered reconnect.
        with self.lock:
            if self.customer_service is not failed_client:
                self._send_session_snapshot()
                return
            try:
                self._reconnect_customer_service_from_cache(reason="send_message_43001")
            except Exception as exc:
                self._set_error(exc)
                raise

    def _persist_login_cache_after_send(self) -> None:
        # Delivery has already been acknowledged. A local cache failure must never
        # enter the transport retry loop or turn a delivered message into a failure.
        try:
            self._persist_current_login_cache()
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING", __name__, "reply.login_cache.persist",
                "message delivered but refreshed login cache could not be saved",
                shop_id=self.shop_id, mall_id=self.mall_id, error=exc,
            )

    def hot_reconnect_customer_service(self, *, reason: str = "scheduled") -> bool:
        # Serialize credential rotation, cache writes and lifecycle changes. The
        # receive callback stays independent; listener startup signals readiness
        # before invoking its error callback, so it cannot wait on this lock.
        with self.lock:
            return self._hot_reconnect_customer_service_locked(reason=reason)

    def _hot_reconnect_customer_service_locked(self, *, reason: str) -> bool:
        old_listener = self.listener if isinstance(self.listener, dict) else None
        if not old_listener or not old_listener.get("client"):
            return False
        self._cancel_ws_reconnect()
        self.runtime_logger.log(
            "INFO",
            __name__,
            "shop.hot_reconnect.start",
            f"starting hot websocket reconnect: {reason}",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
        )
        old_state = {
            "login": self.login,
            "customer_service": self.customer_service,
            "transfer_client": self.transfer_client,
            "goods_service": self.goods_service,
            "order_service": self.order_service,
            "listener": self.listener,
            "session_id": self.session_id,
            "token_result": self.token_result,
        }
        committed = False
        try:
            self._persist_current_login_cache()
            for attempt in range(2):
                new_login = self._login_from_cache()
                new_customer_service = CustomerServiceClient(new_login)
                try:
                    version = new_customer_service.get_version()
                    token_result = self._normalize_token_result(new_customer_service.get_token())
                    access_token = self._access_token_from_token_result(token_result)
                    break
                except Exception as exc:
                    if attempt or not self._send_session_expired(exc) or not self._try_auto_relogin():
                        raise
            new_transfer_client = CustomerTransferClient(new_customer_service)
            new_goods_service = GoodsService(new_login)
            new_order_service = OrderService(new_login)
            new_mall_id = str(token_result.get("mall_id") or self.mall_id or "")
            if new_mall_id:
                self._assert_shop_identity(new_mall_id, source="hot_reconnect")
            session_id = self.db.execute(
                "INSERT INTO shop_sessions (shop_id, status, mall_id, access_token, ws_base_url, token_result) VALUES (%s,'connecting',%s,%s,%s,%s)",
                (
                    self.shop_id,
                    new_mall_id or self.mall_id,
                    self._encrypt_secret_text(access_token),
                    token_result.get("use_ip"),
                    self._encrypt_secret_text(json_dumps(token_result)),
                ),
            )
            def on_candidate_error(error):
                # A failed candidate must not mark the active, older session dead.
                with self.lock:
                    if self.customer_service is new_customer_service:
                        self._on_titan_error(error)

            listener = new_customer_service.start_titan_listener(
                access_token=access_token,
                token_result=token_result,
                on_receive=self._on_titan_receive,
                on_error=on_candidate_error,
                on_pfb_error=self._on_pfb_error,
                daemon=True,
            )
            ready_event = listener.get("ready_event")
            if ready_event and not ready_event.wait(timeout=20):
                raise RuntimeError("new websocket did not become ready")
            if listener.get("ready_error") or listener.get("error"):
                raise listener.get("ready_error") or listener["error"]
            with self.lock:
                self.db.execute(
                    "UPDATE shop_sessions SET status='online', mall_id=%s WHERE id=%s",
                    (new_mall_id or self.mall_id, session_id),
                )
                self.db.execute(
                    "UPDATE shops SET status='online', mall_id=%s, last_error=NULL WHERE id=%s",
                    (new_mall_id or self.mall_id, self.shop_id),
                )
                self.login = new_login
                self.customer_service = new_customer_service
                self.transfer_client = new_transfer_client
                self.goods_service = new_goods_service
                self.order_service = new_order_service
                self.listener = listener
                self.session_id = session_id
                self.token_result = token_result
                self.mall_id = new_mall_id or self.mall_id
                committed = True
            old_client = old_state["listener"].get("client") if isinstance(old_state["listener"], dict) else None
            if old_client:
                old_client.close()
            if old_state.get("session_id"):
                self.db.execute(
                    "UPDATE shop_sessions SET status='replaced', ended_at=NOW() WHERE id=%s",
                    (old_state["session_id"],),
                )
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.runtime_logger.log(
                "INFO",
                __name__,
                "shop.hot_reconnect.success",
                f"hot websocket reconnect succeeded, version={version}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
            )
            return True
        except Exception as exc:
            if committed:
                self.runtime_logger.log(
                    "WARNING", __name__, "shop.hot_reconnect.cleanup_failed",
                    "new websocket is active but reconnect cleanup failed",
                    shop_id=self.shop_id, mall_id=self.mall_id, error=exc,
                )
                return True
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "shop.hot_reconnect.failed",
                "hot websocket reconnect failed; keeping old websocket",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )
            try:
                listener = locals().get("listener")
                if isinstance(listener, dict) and listener.get("client"):
                    listener["client"].close()
            except Exception:
                pass
            self.login = old_state["login"]
            self.customer_service = old_state["customer_service"]
            self.transfer_client = old_state["transfer_client"]
            self.goods_service = old_state["goods_service"]
            self.order_service = old_state["order_service"]
            self.listener = old_state["listener"]
            self.session_id = old_state["session_id"]
            self.token_result = old_state["token_result"]
            return False

    def _save_qrcode(self, uri: str) -> Path:
        QRCODE_DIR.mkdir(parents=True, exist_ok=True)
        path = QRCODE_DIR / f"shop_{self.shop_id}_login_{int(time.time())}.png"
        qrcode_lib.make(uri).save(path)
        return path

    def _on_titan_receive(self, payload: dict[str, Any]) -> None:
        try:
            self._print_received_system_messages(payload)
            display_messages = incoming_display_messages(payload)
            display_messages.sort(key=lambda item: (self._message_ts_ms(item), str(item.get("msg_id") or item.get("client_msg_id") or "")))
            reply_candidates: dict[int, list[tuple[dict[str, Any], int]]] = {}
            for chat_message in display_messages:
                conversation_id, message_id, inserted, should_reply = self._store_chat_message(chat_message)
                if not inserted:
                    continue
                self._capture_inline_product_context(conversation_id, chat_message)
                conversation_row = self._conversation_row(conversation_id)
                if conversation_row:
                    self.hub.publish(
                        {
                            "type": "conversation",
                            "data": conversation_row,
                        }
                    )
                self.hub.publish(
                    {
                        "type": "message",
                        "data": self._message_row(message_id),
                    }
                )
                if should_reply and self._should_schedule_auto_reply(conversation_id):
                    reply_candidates.setdefault(conversation_id, []).append((chat_message, message_id))
            if self._auto_reply_enabled():
                for conversation_id, candidates in reply_candidates.items():
                    self._schedule_auto_reply(conversation_id, candidates)
        except Exception as exc:
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "titan.receive",
                "failed to process titan receive event",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
                context={
                    "payload_type": type(payload).__name__,
                    "display_message_count": len(display_messages) if "display_messages" in locals() else None,
                },
            )

    def _start_latest_conversations_sync(self, *, reason: str) -> None:
        with self.lock:
            self.latest_conversations_sync_index += 1
            sync_id = self.latest_conversations_sync_index
        worker = threading.Thread(
            target=self._sync_latest_conversations_on_online,
            args=(sync_id, reason),
            name=f"shop-{self.shop_id}-latest-conversations",
            daemon=True,
        )
        worker.start()

    def _sync_latest_conversations_on_online(self, sync_id: int, reason: str) -> None:
        customer_service = self.customer_service
        session_id = self.session_id
        if not customer_service:
            return
        shop_row = self._shop_row() or {}
        self.runtime_logger.log(
            "DEBUG",
            __name__,
            "latest_conversations.call",
            "fetching latest conversations",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            context={"sync_id": sync_id, "reason": reason, "offset": 0, "size": 50, "client": 1},
        )
        try:
            data = customer_service.get_latest_conversations(offset=0, size=50, client=1)
            conversations = data.get("conversations") if isinstance(data, dict) else None
            self.runtime_logger.log(
                "DEBUG",
                __name__,
                "latest_conversations.result",
                "latest conversations fetched",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                context={
                    "sync_id": sync_id,
                    "reason": reason,
                    "conversation_count": len(conversations) if isinstance(conversations, list) else None,
                },
            )
            if self.customer_service is not customer_service or self.session_id != session_id:
                self.runtime_logger.log(
                    "DEBUG",
                    __name__,
                    "latest_conversations.skip",
                    "stale session",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    context={"sync_id": sync_id, "reason": reason},
                )
                return
            if not isinstance(conversations, list):
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "latest_conversations.invalid",
                    "latest conversations response has unexpected shape",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    context={"sync_id": sync_id, "reason": reason, "data_type": type(data).__name__},
                )
                return
            pending = [
                item
                for item in conversations
                if self._latest_conversation_needs_reply(item)
                and self._latest_conversation_recent_enough(item)
            ]
            max_pending = 10
            if len(pending) > max_pending:
                self.runtime_logger.log(
                    "INFO",
                    __name__,
                    "latest_conversations.capped",
                    "pending latest conversations capped to avoid reply storm",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    context={"sync_id": sync_id, "reason": reason, "total": len(conversations), "pending": len(pending), "max": max_pending},
                )
                pending = pending[:max_pending]
            self.runtime_logger.log(
                "DEBUG",
                __name__,
                "latest_conversations.filtered",
                "latest conversations filtered",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                context={"sync_id": sync_id, "reason": reason, "total": len(conversations), "pending": len(pending)},
            )
            if not pending:
                self.runtime_logger.log(
                    "INFO",
                    __name__,
                    "latest_conversations.empty",
                    "no pending latest conversations on shop online",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    context={"total": len(conversations)},
                )
                return
            self.runtime_logger.log(
                "INFO",
                __name__,
                "latest_conversations.sync",
                "processing pending latest conversations on shop online",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                context={"total": len(conversations), "pending": len(pending)},
            )
            self._on_titan_receive(self._latest_conversations_event(pending))
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "latest_conversations.error",
                "latest conversations sync failed",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
                context={"sync_id": sync_id, "reason": reason},
            )
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "latest_conversations.sync_failed",
                "failed to sync latest conversations on shop online",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )

    @staticmethod
    def _latest_conversation_recent_enough(message: Any, *, max_age_hours: float = 12.0) -> bool:
        if not isinstance(message, dict):
            return False
        raw_ts = message.get("ts") or message.get("timestamp") or message.get("time")
        try:
            value = int(float(raw_ts))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return False
        if value < 100_000_000_000:
            value *= 1000
        return (time.time() * 1000 - value) <= max_age_hours * 3600 * 1000

    @staticmethod
    def _latest_conversation_needs_reply(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        recipient = message.get("to") if isinstance(message.get("to"), dict) else {}
        if recipient.get("role") == "user":
            return False
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        if sender.get("role") not in (None, "", "user"):
            return False
        if sender.get("uid") in (None, ""):
            return False
        content = message.get("decoded_content") or message.get("content") or ""
        return not ShopRunner._latest_conversation_requests_human(content)

    @staticmethod
    def _latest_conversations_event(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "event",
            "event": {
                "type": "message",
                "source": "latest_conversations",
                "messages": [
                    {
                        "message": ShopRunner._normalize_latest_conversation_message(message),
                        "source": "latest_conversations",
                    }
                    for message in messages
                ],
            },
        }

    @staticmethod
    def _normalize_latest_conversation_message(message: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(message)
        normalized["_source"] = "latest_conversations"
        recipient = normalized.get("to") if isinstance(normalized.get("to"), dict) else {}
        sender = normalized.get("from") if isinstance(normalized.get("from"), dict) else {}
        if recipient.get("role") != "user" and sender.get("uid") not in (None, ""):
            normalized["from"] = {**sender, "role": "user"}
        return normalized

    @staticmethod
    def _latest_conversation_requests_human(content: Any) -> bool:
        text = str(content or "").strip().lower()
        return any(
            keyword in text
            for keyword in (
                "人工",
                "人工客服",
                "转人工",
                "找人工",
                "真人",
                "投诉",
                "平台介入",
            )
        )

    def _print_received_system_messages(self, payload: dict[str, Any]) -> None:
        printed: set[tuple[Any, ...]] = set()
        for message in incoming_chat_messages(payload):
            if not is_system_notice_message(message):
                continue
            key = message_dedupe_key(message)
            if key in printed:
                continue
            printed.add(key)
            self._print_system_message(message)

    def _print_system_message(self, message: dict[str, Any]) -> None:
        self.runtime_logger.log(
            "DEBUG",
            __name__,
            "titan.system_message",
            "system message received",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            context={
                "message_type": message.get("type") or message.get("template_name"),
                "has_info": isinstance(message.get("info"), dict),
                "content_length": len(str(message.get("decoded_content") or message.get("content") or "")),
            },
        )

    @staticmethod
    def _system_message_text(message: dict[str, Any]) -> str:
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        text = (
            message.get("decoded_content")
            or message.get("content")
            or info.get("mall_content")
            or info.get("mall_item_content")
            or info.get("content")
            or message.get("template_name")
            or ""
        )
        return str(text).strip() or "[系统消息]"

    def _schedule_auto_reply(self, conversation_id: int, candidates: list[tuple[dict[str, Any], int]]) -> None:
        if not candidates:
            return
        user_uid = self._candidate_user_uid(candidates)
        for message, _ in candidates:
            self._capture_inline_product_context(conversation_id, message)
        with self.reply_state_lock:
            self.pending_reply_messages.setdefault(conversation_id, []).extend(candidates)
            existing = self.reply_workers.get(conversation_id)
            if existing and (
                (hasattr(existing, "is_alive") and existing.is_alive())
                or (hasattr(existing, "done") and not existing.done())
            ):
                return
            if self.manager and getattr(self.manager, "reply_executor", None):
                worker = self.manager.reply_executor.submit(self._reply_worker, conversation_id)
            else:
                worker = threading.Thread(
                    target=self._reply_worker,
                    args=(conversation_id,),
                    name=f"shop-{self.shop_id}-reply-{conversation_id}-{user_uid or 'unknown'}",
                    daemon=True,
                )
            self.reply_workers[conversation_id] = worker
        if hasattr(worker, "start"):
            worker.start()
        self.runtime_logger.log(
            "INFO",
            __name__,
            "reply.worker.start",
            "started independent reply worker for conversation",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
            context={"pending_count": len(candidates), "worker_name": getattr(worker, "name", "reply-pool")},
        )

    def _reply_worker(self, conversation_id: int) -> None:
        while True:
            time.sleep(self.reply_batch_delay)
            with self.reply_state_lock:
                batch = self.pending_reply_messages.pop(conversation_id, [])
            if not batch:
                # 竞态窗口：新消息可能在"弹出空批次"与"线程退出"之间到达。
                # 退出锁后短暂等待，二次确认仍为空才真正退出。
                time.sleep(0.2)
                with self.reply_state_lock:
                    batch = self.pending_reply_messages.pop(conversation_id, [])
                    if not batch:
                        current = threading.current_thread()
                        existing = self.reply_workers.get(conversation_id)
                        if existing is current or isinstance(existing, Future):
                            self.reply_workers.pop(conversation_id, None)
                        return
            batch.sort(key=lambda item: (self._message_ts_ms(item[0]), item[1]))
            chat_message, message_id = batch[-1]
            user_uid = self._candidate_user_uid(batch)
            self.runtime_logger.log(
                "INFO",
                __name__,
                "reply.batch.start",
                "processing reply batch for conversation",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                request_id=message_id,
                context={"batch_size": len(batch), "worker_name": threading.current_thread().name},
            )
            try:
                self._send_auto_reply(
                    chat_message,
                    conversation_id,
                    message_id,
                    batch_messages=[item[0] for item in batch],
                )
            except Exception as exc:
                self.runtime_logger.log(
                    "ERROR",
                    __name__,
                    "titan.reply",
                    "failed to send auto reply",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    conversation_id=conversation_id,
                    error=exc,
                )
                user_uid = str((chat_message.get("from") or {}).get("uid") or "")
                if self._is_latest_conversations_message(chat_message):
                    continue
                if not self._is_force_ai_reply():
                    self._handle_need_human(conversation_id, user_uid, "reply_error")

    @staticmethod
    def _candidate_user_uid(candidates: list[tuple[dict[str, Any], int]]) -> str:
        for message, _ in candidates:
            uid = str((message.get("from") or {}).get("uid") or "")
            if uid:
                return uid
        return ""

    @staticmethod
    def _is_user_source_message(message: dict[str, Any]) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("template_name") == "user_source":
            return True
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        content = str(message.get("decoded_content") or message.get("content") or info.get("title") or "")
        return bool(info.get("goods_info") and "当前用户来自" in content)

    @staticmethod
    def _extract_product_context(message: dict[str, Any]) -> dict[str, Any] | None:
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        goods_info = info.get("goods_info") if isinstance(info.get("goods_info"), dict) else {}
        if not goods_info:
            return None
        goods_id = goods_info.get("goods_id") or goods_info.get("goodsId")
        if not goods_id:
            return None
        return {
            "title": info.get("title") or message.get("content") or message.get("decoded_content") or "",
            "goods_id": str(goods_id),
            "goods_name": goods_info.get("goods_name") or goods_info.get("goodsName") or "",
            "goods_thumb_url": goods_info.get("goods_thumb_url") or goods_info.get("thumb_url") or "",
            "mall_link_url": goods_info.get("mall_link_url") or goods_info.get("link_url") or "",
            "total_amount": goods_info.get("total_amount"),
            "message_ts": message.get("ts"),
            "raw": message,
        }

    def _capture_inline_product_context(self, conversation_id: int, message: dict[str, Any]) -> None:
        if not self._is_user_source_message(message):
            return
        product_context = self._extract_product_context(message)
        if product_context:
            self.latest_product_context[int(conversation_id)] = product_context

    def _active_mall_id(self) -> str:
        token_result = self.token_result or {}
        mall_id = (
            self.mall_id
            or token_result.get("mall_id")
            or token_result.get("mallId")
            or token_result.get("mallID")
        )
        if not mall_id:
            row = self.db.query_one("SELECT mall_id FROM shops WHERE id=%s", (self.shop_id,))
            mall_id = row.get("mall_id") if row else ""
        return str(mall_id or "")

    def _assert_shop_identity(self, new_mall_id: str, *, source: str, allow_mall_change: bool = False) -> None:
        new_mall_id = str(new_mall_id or "")
        if not new_mall_id:
            return
        shop = self.db.query_one("SELECT mall_id, name FROM shops WHERE id=%s", (self.shop_id,))
        old_mall_id = str((shop or {}).get("mall_id") or "")
        owner = self.db.query_one(
            "SELECT id, name FROM shops WHERE mall_id=%s AND id<>%s LIMIT 1",
            (new_mall_id, self.shop_id),
        )
        if owner:
            self._invalidate_login_cache()
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "shop.identity_duplicate",
                "PDD mall identity is already bound to another shop card; refusing to reuse it",
                shop_id=self.shop_id,
                mall_id=new_mall_id,
                context={
                    "shop_name": (shop or {}).get("name"),
                    "owner_shop_id": owner.get("id"),
                    "owner_shop_name": owner.get("name"),
                    "actual_mall_id": new_mall_id,
                    "source": source,
                },
            )
            raise RuntimeError(
                f"店铺登录身份已被其他店铺占用：mall_id={new_mall_id} 已绑定到"
                f"店铺 {owner.get('id')}（{owner.get('name')}）。请重新登录正确店铺。"
            )
        if not old_mall_id or old_mall_id == new_mall_id:
            return
        if allow_mall_change:
            self._handle_mall_change(new_mall_id)
            return
        self._invalidate_login_cache()
        self.runtime_logger.log(
            "ERROR",
            __name__,
            "shop.identity_mismatch",
            "login cache belongs to a different PDD mall; refusing to bring this shop online",
            shop_id=self.shop_id,
            mall_id=new_mall_id,
            context={
                "shop_name": (shop or {}).get("name"),
                "expected_mall_id": old_mall_id,
                "actual_mall_id": new_mall_id,
                "source": source,
            },
        )
        raise RuntimeError(
            f"店铺登录身份不匹配：当前店铺绑定 mall_id={old_mall_id}，"
            f"但登录缓存属于 mall_id={new_mall_id}。请清除后重新登录该店铺。"
        )

    def _handle_mall_change(self, new_mall_id: str) -> None:
        new_mall_id = str(new_mall_id or "")
        if not new_mall_id:
            return
        shop = self.db.query_one("SELECT mall_id FROM shops WHERE id=%s", (self.shop_id,))
        old_mall_id = str((shop or {}).get("mall_id") or "")
        if not old_mall_id or old_mall_id == new_mall_id:
            return

        self.db.execute(
            """
            UPDATE conversations
            SET mall_id=%s
            WHERE shop_id=%s AND (mall_id IS NULL OR mall_id='')
            """,
            (old_mall_id, self.shop_id),
        )
        self.runtime_logger.log(
            "WARNING",
            __name__,
            "shop.mall_changed",
            "shop card logged in with a different mall_id; old conversations were locked to the previous mall",
            shop_id=self.shop_id,
            mall_id=new_mall_id,
            context={"old_mall_id": old_mall_id, "new_mall_id": new_mall_id},
        )

    def _store_unknown_event(self, payload: dict[str, Any]) -> int | None:
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("type") not in {"message", "notify", "notifyData", "notifyInner", "frame"}:
            return None
        user_uid = self.customer_service.extract_customer_uid(event) if self.customer_service else ""
        if not user_uid:
            user_uid = "unknown"
        mall_id = self._active_mall_id() or None
        conversation = self.db.query_one(
            "SELECT * FROM conversations WHERE shop_id=%s AND user_uid=%s AND mall_id <=> %s",
            (self.shop_id, user_uid, mall_id),
        )
        if conversation:
            conversation_id = int(conversation["id"])
            self.db.execute(
                "UPDATE conversations SET mall_id=%s, last_message_preview=%s, unread_count=unread_count+1 WHERE id=%s",
                (mall_id, f"[{event.get('type') or 'unknown'}]", conversation_id),
            )
        else:
            conversation_id = self.db.execute(
                """
                INSERT INTO conversations (shop_id, mall_id, conv_id, user_uid, last_message_preview, unread_count)
                VALUES (%s,%s,%s,%s,%s,1)
                """,
                (self.shop_id, mall_id, user_uid, user_uid, f"[{event.get('type') or 'unknown'}]"),
            )
        return self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, user_uid, sender_role, kind, content, raw_json, status)
            VALUES (%s,%s,'system',%s,'system','unknown',%s,%s,'received')
            """,
            (self.shop_id, conversation_id, user_uid, f"[{event.get('type') or 'unknown'}]", json_dumps(payload)),
        )

    @staticmethod
    def _message_ts_ms(message: dict[str, Any]) -> int:
        raw_ts = message.get("ts") or message.get("timestamp") or message.get("time")
        try:
            value = int(float(raw_ts))
        except (TypeError, ValueError):
            return int(time.time() * 1000)
        if value <= 0:
            return int(time.time() * 1000)
        return value * 1000 if value < 100_000_000_000 else value

    def _store_chat_message(self, user_message: dict[str, Any]) -> tuple[int, int, bool, bool]:
        existing = self._existing_message_row(user_message)
        if existing:
            return int(existing["conversation_id"] or 0), int(existing["id"]), False, False

        summary = user_message_summary(user_message)
        message_at = self._message_ts_ms(user_message)
        user_uid = summary["uid"]
        is_system = is_system_notice_message(user_message)
        direction = "system" if is_system else "inbound"
        sender_role = "system" if is_system else "user"
        context = user_message.get("_reply_context") if isinstance(user_message.get("_reply_context"), dict) else {}
        conv_id = context.get("conv_id") or user_message.get("conv_id") or user_uid
        mall_id = self._active_mall_id() or None
        conversation = self.db.query_one(
            "SELECT * FROM conversations WHERE shop_id=%s AND user_uid=%s AND mall_id <=> %s",
            (self.shop_id, user_uid, mall_id),
        )
        if conversation:
            conversation_id = int(conversation["id"])
            nickname = summary.get("nickname") or conversation.get("nickname") or ""
            self.db.execute(
                """
                UPDATE conversations
                SET mall_id=%s, conv_id=%s, chat_type_id=%s, chat_type=%s, nickname=%s,
                    last_message_preview=IF(last_message_at IS NULL OR last_message_at<=%s, %s, last_message_preview),
                    last_message_at=IF(last_message_at IS NULL OR last_message_at<=%s, %s, last_message_at),
                    unread_count=unread_count+%s
                WHERE id=%s
                """,
                (
                    mall_id,
                    str(conv_id or ""),
                    str(context.get("chat_type_id") or ""),
                    str(context.get("chat_type") or ""),
                    nickname,
                    message_at,
                    str(summary.get("content") or "")[:512],
                    message_at,
                    message_at,
                    0 if is_system else 1,
                    conversation_id,
                ),
            )
        else:
            try:
                conversation_id = self.db.execute(
                    """
                    INSERT INTO conversations
                    (shop_id, mall_id, conv_id, chat_type_id, chat_type, user_uid, nickname,
                     last_message_preview, last_message_at, unread_count)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.shop_id,
                        mall_id,
                        str(conv_id or ""),
                        str(context.get("chat_type_id") or ""),
                        str(context.get("chat_type") or ""),
                        user_uid,
                        summary.get("nickname") or "",
                        str(summary.get("content") or "")[:512],
                        message_at,
                        0 if is_system else 1,
                    ),
                )
            except IntegrityError:
                # 双连接/并发下唯一键 (shop_id, mall_id, user_uid) 冲突：
                # 回查已存在的会话，按幂等继续入库
                conversation = self.db.query_one(
                    "SELECT * FROM conversations WHERE shop_id=%s AND user_uid=%s AND mall_id <=> %s",
                    (self.shop_id, user_uid, mall_id),
                )
                if not conversation:
                    raise
                conversation_id = int(conversation["id"])

        try:
            message_id = self.db.execute(
                """
                INSERT INTO messages
                (shop_id, conversation_id, direction, msg_id, client_msg_id, user_uid,
                 sender_role, message_type, kind, content, goods_json, size_json, raw_json, message_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    self.shop_id,
                    conversation_id,
                    direction,
                    str(user_message.get("msg_id") or ""),
                    str(user_message.get("client_msg_id") or ""),
                    user_uid,
                    sender_role,
                    user_message.get("type"),
                    summary.get("kind") or "unknown",
                    str(summary.get("content") or ""),
                    json_dumps(summary.get("goods")) if summary.get("goods") else None,
                    json_dumps(summary.get("size")) if summary.get("size") else None,
                    json_dumps(user_message),
                    message_at,
                ),
            )
        except IntegrityError:
            # 消息唯一索引 (shop_id, msg_id_key) 冲突：双连接重复推送同一消息，
            # 幂等返回已入库的那一条
            existing = self._existing_message_row(user_message)
            if existing:
                return int(existing["conversation_id"] or 0), int(existing["id"]), False, False
            raise
        return conversation_id, message_id, True, not is_system

    def _existing_message_row(self, message: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = str(message.get("msg_id") or "")
        if msg_id:
            row = self.db.query_one(
                "SELECT id, conversation_id FROM messages WHERE shop_id=%s AND msg_id=%s LIMIT 1",
                (self.shop_id, msg_id),
            )
            if row:
                return row
        client_msg_id = str(message.get("client_msg_id") or "")
        if client_msg_id:
            row = self.db.query_one(
                "SELECT id, conversation_id FROM messages WHERE shop_id=%s AND client_msg_id=%s LIMIT 1",
                (self.shop_id, client_msg_id),
            )
            if row:
                return row
        return None

    def _auto_reply_enabled(self) -> bool:
        row = self.db.query_one(
            "SELECT auto_reply_enabled, status FROM shops WHERE id=%s",
            (self.shop_id,),
        )
        if not row or not row.get("auto_reply_enabled"):
            return False
        # 防"假在线"：DB 被其他进程标为 offline/error 但本进程 WS 仍存活时，
        # 不得继续自动回复（前端已显示离线）。
        if str(row.get("status") or "") not in {"online", "connecting"}:
            return False
        listener = self.listener
        if isinstance(listener, dict) and listener.get("client") is not None:
            return True
        return False

    def _conversation_bot_reply_enabled(self, conversation_id: int) -> bool:
        row = self.db.query_one(
            "SELECT bot_reply_enabled FROM conversations WHERE id=%s AND shop_id=%s",
            (conversation_id, self.shop_id),
        )
        return bool(row and row.get("bot_reply_enabled"))

    def _should_schedule_auto_reply(self, conversation_id: int) -> bool:
        return (
            self._auto_reply_enabled()
            and self._conversation_bot_reply_enabled(conversation_id)
            and not self._is_conversation_transferred(conversation_id)
        )

    def _is_conversation_transferred(self, conversation_id: int) -> bool:
        row = self.db.query_one(
            "SELECT transferred_at FROM conversations WHERE id=%s",
            (conversation_id,),
        )
        return row and row.get("transferred_at") is not None

    def _load_sendable_conversation(self, conversation_id: int) -> dict[str, Any]:
        conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise ValueError(f"conversation not found: {conversation_id}")
        if int(conversation["shop_id"]) != self.shop_id:
            raise ValueError(f"conversation does not belong to shop: {conversation_id}")

        conversation_mall_id = str(conversation.get("mall_id") or "")
        current_mall_id = self._active_mall_id()
        if conversation_mall_id and current_mall_id and conversation_mall_id != current_mall_id:
            raise RuntimeError("\u5e97\u94fa\u5df2\u66f4\u6362\uff0c\u8be5\u5386\u53f2\u4f1a\u8bdd\u5c5e\u4e8e\u65e7\u5e97\u94fa\uff0c\u65e0\u6cd5\u53d1\u9001\u6d88\u606f")
        if self._is_conversation_transferred(conversation_id):
            raise RuntimeError("\u4f1a\u8bdd\u5df2\u8f6c\u63a5\uff0c\u65e0\u6cd5\u53d1\u9001\u6d88\u606f\uff0c\u8bf7\u7b49\u5f85\u7528\u6237\u56de\u590d\u540e\u91cd\u8bd5")
        return conversation

    def _is_force_ai_reply(self) -> bool:
        row = self.db.query_one("SELECT auto_reply_enabled, force_ai_reply FROM shops WHERE id=%s", (self.shop_id,))
        return bool(row and row.get("auto_reply_enabled") and row.get("force_ai_reply"))

    def _get_shop_greeting(self) -> str | None:
        row = self.db.query_one("SELECT greeting_message FROM shops WHERE id=%s", (self.shop_id,))
        return row.get("greeting_message") if row else None

    def _conversation_has_outbound(self, conversation_id: int) -> bool:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM messages WHERE conversation_id=%s AND direction='outbound'",
            (conversation_id,),
        )
        return bool(row and row.get("cnt", 0) > 0)

    def _get_shop_transfer_csids(self) -> list[str]:
        row = self.db.query_one("SELECT transfer_csids FROM shops WHERE id=%s", (self.shop_id,))
        if not row or not row.get("transfer_csids"):
            return []
        try:
            csids = json.loads(row["transfer_csids"])
            return [str(csid).strip() for csid in csids if str(csid).strip()] if isinstance(csids, list) else []
        except (TypeError, ValueError):
            return []

    def _set_conversation_bot_reply(self, conversation_id: int, enabled: bool) -> None:
        self.db.execute(
            "UPDATE conversations SET bot_reply_enabled=%s WHERE id=%s AND shop_id=%s",
            (int(enabled), conversation_id, self.shop_id),
        )

    def _mark_human_attention(self, conversation_id: int, user_uid: str, reason: str) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET bot_reply_enabled=0,
                human_attention_required=1,
                human_attention_reason=%s,
                human_attention_at=NOW()
            WHERE id=%s AND shop_id=%s
            """,
            (reason, conversation_id, self.shop_id),
        )
        row = self.db.query_one(
            """
            SELECT c.*, s.name AS shop_name, s.status AS shop_status,
                   s.auto_reply_enabled AS shop_auto_reply_enabled
            FROM conversations c
            JOIN shops s ON s.id=c.shop_id
            WHERE c.id=%s
            """,
            (conversation_id,),
        )
        if row:
            self.hub.publish({"type": "conversation_attention", "data": row})
        self.runtime_logger.log(
            "WARNING",
            __name__,
            "conversation.human_attention",
            f"conversation requires human attention: {reason}",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
        )

    def _handle_need_human(self, conversation_id: int, user_uid: str, reason: str) -> None:
        if self._try_auto_transfer(conversation_id, user_uid, reason):
            return
        self._mark_human_attention(conversation_id, user_uid, reason)

    def _try_auto_transfer(self, conversation_id: int, user_uid: str, reason: str) -> bool:
        csids = self._get_shop_transfer_csids()
        if not csids:
            return False
        if not self.transfer_client:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "auto_transfer.skip",
                f"cannot auto-transfer: no transfer client (reason={reason})",
                shop_id=self.shop_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
            )
            return False
        for index, csid in enumerate(csids, start=1):
            try:
                self.transfer(
                    conversation_id=conversation_id,
                    csid=csid,
                    remark=f"\u81ea\u52a8\u8f6c\u63a5\uff08{reason}\uff0c\u7b2c{index}\u4f18\u5148\u7ea7\uff09",
                )
                self.runtime_logger.log(
                    "INFO",
                    __name__,
                    "auto_transfer.success",
                    f"auto transfer to csid={csid} (reason={reason}, priority={index}/{len(csids)})",
                    shop_id=self.shop_id,
                    conversation_id=conversation_id,
                    user_uid=user_uid,
                )
                return True
            except Exception as exc:
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "auto_transfer.retry",
                    f"auto transfer to csid={csid} failed, trying next (priority={index}/{len(csids)}): {exc}",
                    shop_id=self.shop_id,
                    conversation_id=conversation_id,
                    user_uid=user_uid,
                    error=exc,
                )
        self.runtime_logger.log(
            "ERROR",
            __name__,
            "auto_transfer.all_failed",
            f"auto transfer failed for all {len(csids)} csids",
            shop_id=self.shop_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
        )
        return False

    def _send_auto_reply(
        self,
        user_message: dict[str, Any],
        conversation_id: int,
        message_id: int,
        *,
        batch_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._should_schedule_auto_reply(conversation_id):
            return

        batch = list(batch_messages or [user_message])
        batch.sort(key=lambda item: (self._message_ts_ms(item), str(item.get("msg_id") or item.get("client_msg_id") or "")))
        dedupe_keys = [message_dedupe_key(item) for item in batch]
        with self.reply_state_lock:
            unreplied_keys = [key for key in dedupe_keys if key not in self.replied_keys]
            if not unreplied_keys:
                return
            self.replied_keys.update(unreplied_keys)
            if len(self.replied_keys) > ShopRunner.MAX_REPLIED_KEYS:
                # 有界去重：超限时清掉约 1/3（消息入库层的 msg_id 唯一索引
                # 仍兜底防重，所以清空部分键不会造成重复回复）
                for _ in range(ShopRunner.MAX_REPLIED_KEYS // 3):
                    self.replied_keys.pop()

        user_uid = str((user_message.get("from") or {}).get("uid") or "")
        content_parts = [
            str(user_message_summary(item).get("content") or "").strip()
            for item in batch
        ]
        content = "\n".join(part for part in content_parts if part)
        if not content:
            content = str(user_message_summary(user_message).get("content") or "")
        is_batch = len(batch) > 1
        is_latest_conversations_batch = any(self._is_latest_conversations_message(item) for item in batch)
        product_context = self.latest_product_context.get(int(conversation_id))
        business_context = self._build_business_context_for_reply(
            conversation_id=conversation_id,
            user_uid=user_uid,
            user_content=content,
            product_context=product_context,
        )
        if is_latest_conversations_batch:
            business_context = self._append_latest_conversation_notice(business_context)
        has_realtime_context = bool(business_context)

        greeting = self._get_shop_greeting()
        if greeting and not is_latest_conversations_batch and not self._conversation_has_outbound(conversation_id):
            try:
                self.send_reply(conversation_id=conversation_id, content=greeting, source_message_id=None, auto=False)
            except Exception:
                pass

        preflight_return_record = self._handle_return_record_intent(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            user_content=content,
            intent={
                "reply": "",
                "intent_code": "custom",
                "raw_intent_code": "return_record_preflight",
                "confidence": 1,
                "resolution_status": "need_action",
                "slots": {},
                "actions": [],
                "raw": {"source": "preflight"},
            },
            knowledge_hits=[],
        )
        if preflight_return_record:
            return

        if is_latest_conversations_batch and self._is_weak_latest_conversation_content(content):
            self._send_latest_conversation_clarification(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                content=content,
                reason="weak_content",
            )
            return

        if not is_batch and not has_realtime_context:
            qa_match = self.knowledge.find_qa_match_for_shop(shop_id=self.shop_id, query=content)
            if qa_match:
                self._send_knowledge_qa_reply(conversation_id, message_id, user_uid, qa_match)
                return

            fast_reply = detect_fast_reply(content)
            if fast_reply:
                self._send_cached_or_fast_reply(conversation_id, message_id, user_uid, fast_reply, source="fast_path")
                return

            if self.reply_cache:
                cached = self.reply_cache.get(content, self.shop_id)
                if cached:
                    if self._is_force_ai_reply():
                        cached = self._suppress_unwanted_transfer(content, cached, conversation_id)
                    self._send_cached_or_fast_reply(
                        conversation_id, message_id, user_uid,
                        str(cached.get("reply") or ""), source="cache",
                        intent=cached,
                    )
                    return

        history = self._llm_history(conversation_id=conversation_id, current_message_id=message_id)
        compressed = build_conversation_messages(history)

        knowledge_hits = self.knowledge.search_for_shop(
            shop_id=self.shop_id,
            query=self._knowledge_query(history),
            top_k=5,
        )

        shop_notes = self.note_service.get_items_for_shop(self.shop_id)

        quota_owner, quota_exhausted = self._reserve_creator_llm_quota("大模型回复次数已达上限，所有店铺已下线")
        try:
            intent = analyze_customer_intent(
                compressed,
                knowledge_hits=knowledge_hits,
                shop_notes=shop_notes or None,
                business_context=business_context or None,
                llm_client=self.llm_client,
            )
        except Exception as exc:
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "reply.intent",
                "failed to analyze intent",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                request_id=message_id,
                error=exc,
            )
            if is_latest_conversations_batch:
                self._send_latest_conversation_clarification(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    user_uid=user_uid,
                    content=content,
                    reason="llm_error",
                )
            elif not self._is_force_ai_reply():
                self._handle_need_human(conversation_id, user_uid, "llm_error")
            if quota_owner and quota_exhausted:
                self._quota_exceeded(quota_owner)
            return

        handled_return_record = self._handle_return_record_intent(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            user_content=content,
            intent=intent,
            knowledge_hits=knowledge_hits,
        )
        if handled_return_record:
            if quota_owner and quota_exhausted:
                self._quota_exceeded(quota_owner)
            return

        if is_latest_conversations_batch and self._intent_needs_human(intent):
            intent = self._latest_conversation_need_more_info_intent(intent)

        if self._intent_needs_human(intent) and not self._is_force_ai_reply():
            intent_event_id = self._store_intent_event(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                intent=intent,
                knowledge_hits=knowledge_hits,
            )
            self.actions.create_and_dispatch(
                shop_id=self.shop_id,
                conversation_id=conversation_id,
                message_id=message_id,
                intent_event_id=intent_event_id,
                user_uid=user_uid,
                actions=intent.get("actions") or [],
                slots=intent.get("slots") or {},
            )
            self._handle_need_human(conversation_id, user_uid, "transfer_to_human")
            if quota_owner and quota_exhausted:
                self._quota_exceeded(quota_owner)
            return

        if self._is_force_ai_reply():
            intent = self._suppress_unwanted_transfer(content, intent, conversation_id)
        intent["reply"] = clean_reply_text(intent.get("reply") or "")

        try:
            final_reply = self._send_auto_text_reply(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                content=intent["reply"],
                history=compressed,
                knowledge_hits=knowledge_hits,
                shop_notes=shop_notes or None,
                business_context=business_context or None,
            )
            intent["reply"] = final_reply
        finally:
            if quota_owner and quota_exhausted:
                self._quota_exceeded(quota_owner)
        if not is_batch and self.reply_cache and self._is_cacheable(content, intent):
            self.reply_cache.put(content, self.shop_id, intent)
        intent_event_id = self._store_intent_event(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            intent=intent,
            knowledge_hits=knowledge_hits,
        )
        self.actions.create_and_dispatch(
            shop_id=self.shop_id,
            conversation_id=conversation_id,
            message_id=message_id,
            intent_event_id=intent_event_id,
            user_uid=user_uid,
            actions=intent.get("actions") or [],
            slots=intent.get("slots") or {},
        )

        if ShopRunner._intent_needs_human(intent):
            if not self._is_force_ai_reply():
                self._handle_need_human(conversation_id, user_uid, "transfer_to_human")

    def _handle_return_record_intent(
        self,
        *,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        user_content: str,
        intent: dict[str, Any],
        knowledge_hits: list[dict[str, Any]],
    ) -> bool:
        result = self.return_records.handle_intent(
            shop_id=self.shop_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            user_content=user_content,
            intent=intent,
            order_lookup=lambda order_no: self._lookup_order_snapshot(user_uid=user_uid, order_no=order_no),
        )
        if not result or not result.handled:
            return False

        try:
            final_reply = self._send_auto_text_reply(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                content=result.reply,
                knowledge_hits=knowledge_hits,
                context_instruction="原回复是业务登记结果，重写时必须保留登记成功、需要跟进处理等核心含义，不要新增承诺。",
            )
            result.intent["reply"] = final_reply
        finally:
            self._store_intent_event(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                intent=result.intent,
                knowledge_hits=knowledge_hits,
            )
        if self._customer_requested_human(user_content) and not self._is_force_ai_reply():
            self._handle_need_human(conversation_id, user_uid, "customer_requested_human")
        return True

    def _send_knowledge_qa_reply(self, conversation_id: int, message_id: int, user_uid: str, qa_item: dict[str, Any]) -> None:
        reply = clean_reply_text(str(qa_item.get("reply") or ""))
        image_base64 = str(qa_item.get("image_base64") or "")
        optimized_reply = ""
        quota_owner = None
        quota_exhausted = False
        if reply:
            optimized_reply, quota_owner, quota_exhausted = self._optimize_qa_reply(reply)
            try:
                self._send_auto_text_reply(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    user_uid=user_uid,
                    content=optimized_reply,
                    context_instruction="原回复来自知识库，重写时必须保持原意和事实，不要新增知识库未提供的信息。",
                )
                if image_base64:
                    self.send_image(conversation_id=conversation_id, image_base64=image_base64)
            finally:
                if quota_owner and quota_exhausted:
                    self._quota_exceeded(quota_owner)
        elif image_base64:
            self.send_image(conversation_id=conversation_id, image_base64=image_base64)
        self.runtime_logger.log(
            "INFO",
            __name__,
            "reply.knowledge_qa",
            "reply via knowledge qa item",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
            context={
                "knowledge_base_id": qa_item.get("knowledge_base_id"),
                "qa_item_id": qa_item.get("id"),
                "match_score": qa_item.get("match_score"),
                "has_reply": bool(optimized_reply),
                "has_image": bool(image_base64),
            },
        )

    def conversation_context(self, conversation_id: int) -> dict[str, Any]:
        conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise ValueError(f"conversation not found: {conversation_id}")
        if int(conversation["shop_id"]) != self.shop_id:
            raise ValueError(f"conversation does not belong to shop: {conversation_id}")

        user_uid = str(conversation.get("user_uid") or "")
        orders_status = "unavailable"
        orders_message = "店铺未在线，暂时无法查询订单记录。"
        orders: list[dict[str, Any]] = []
        service = self._order_service()
        if service:
            try:
                order_data = service.get_user_orders(user_uid, page_no=1, page_size=10)
                orders = self._order_cards(order_data)
                orders_status = "ready" if orders else "empty"
                orders_message = "" if orders else "未查到该用户近期订单记录。"
            except Exception as exc:
                orders_status = "empty" if self._is_not_found_error(exc) else "failed"
                orders_message = "未查到该用户近期订单记录。" if orders_status == "empty" else f"订单记录查询失败：{exc}"
                if orders_status == "failed":
                    self.runtime_logger.log(
                        "WARNING",
                        __name__,
                        "context_panel.orders",
                        "failed to fetch orders for conversation context panel",
                        shop_id=self.shop_id,
                        mall_id=self.mall_id,
                        conversation_id=conversation_id,
                        user_uid=user_uid,
                        error=exc,
                    )

        return {
            "conversation_id": conversation_id,
            "user_uid": user_uid,
            "recent_goods": self._recent_product_cards(conversation_id),
            "orders": orders,
            "orders_status": orders_status,
            "orders_message": orders_message,
        }

    def _build_business_context_for_reply(
        self,
        *,
        conversation_id: int,
        user_uid: str,
        user_content: str,
        product_context: dict[str, Any] | None,
    ) -> str:
        context: dict[str, Any] = {
            "schema": "openkefu.realtime_business_context.v1",
            "conversation_id": conversation_id,
            "user_uid": user_uid,
            "instructions": [
                "所有订单、物流、退款、售后、商品价格、库存、规格、上架状态等事实只能依据本 JSON 中明确提供的 raw_response 或 raw_message。",
                "当 status 为 not_found 时，必须如实告知未查询到对应订单或商品，不得编造任何缺失信息。",
                "当 status 为 failed 或 unavailable 时，必须说明暂时无法核实，不得把失败原因当作业务事实。",
            ],
            "current_viewed_product": None,
            "orders": [],
            "products": [],
        }
        if product_context:
            compact = self._compact_product_source_context(product_context)
            context["current_viewed_product"] = {
                "source": "user_source_message",
                "status": "found",
                "query": {"goods_id": compact.get("goods_id") or ""},
                "summary": compact,
                "raw_message": product_context.get("raw") or product_context,
                "instruction": "这是顾客当前正在浏览的商品来源消息。可用于识别顾客所说的“这款/这个/当前商品”，但商品价格、库存、规格等详情仍优先依据 products 中的商品详情接口 raw_response。",
            }

        wants_order = self._looks_like_order_question(user_content)
        wants_product = self._looks_like_product_question(user_content, has_product_context=bool(product_context))

        if wants_order:
            context["orders"].append(self._order_context_for_llm(user_uid=user_uid, conversation_id=conversation_id))

        if wants_product:
            goods_ids = self._goods_ids_for_reply(user_content, product_context)
            for goods_id in goods_ids[:2]:
                context["products"].append(self._goods_context_for_llm(goods_id=goods_id, conversation_id=conversation_id))

        if not context["current_viewed_product"] and not context["orders"] and not context["products"]:
            return ""
        return json.dumps(context, ensure_ascii=False, default=str)

    @staticmethod
    def _is_latest_conversations_message(message: dict[str, Any]) -> bool:
        return isinstance(message, dict) and message.get("_source") == "latest_conversations"

    @staticmethod
    def _is_weak_latest_conversation_content(content: str) -> bool:
        text = str(content or "").strip()
        meaningful = [
            ch
            for ch in text
            if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
        ]
        return len(meaningful) <= 2

    @staticmethod
    def _latest_conversation_clarification_reply() -> str:
        return "亲，刚刚这条消息我这边没能确认具体需求，麻烦您再发一下要咨询的问题或补充一下具体需求，我马上帮您处理。"

    def _latest_conversation_need_more_info_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(intent or {})
        sanitized["reply"] = self._latest_conversation_clarification_reply()
        sanitized["intent_code"] = sanitized.get("intent_code") or "unknown"
        sanitized["resolution_status"] = "need_more_info"
        try:
            confidence = float(sanitized.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        sanitized["confidence"] = min(confidence, 0.6)
        sanitized["actions"] = [
            item
            for item in sanitized.get("actions") or []
            if not (isinstance(item, dict) and item.get("type") == "transfer_to_human")
        ]
        raw = sanitized.get("raw") if isinstance(sanitized.get("raw"), dict) else {}
        sanitized["raw"] = {
            **raw,
            "latest_conversations_guard": "converted_transfer_to_need_more_info",
        }
        return sanitized

    def _send_latest_conversation_clarification(
        self,
        *,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        content: str,
        reason: str,
    ) -> None:
        intent = {
            "reply": self._latest_conversation_clarification_reply(),
            "intent_code": "unknown",
            "raw_intent_code": "latest_conversations_clarification",
            "confidence": 0.4,
            "resolution_status": "need_more_info",
            "slots": {},
            "actions": [],
            "raw": {
                "source": "latest_conversations",
                "reason": reason,
                "content": content,
            },
        }
        final_reply = self._send_auto_text_reply(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            content=intent["reply"],
            context_instruction="这条消息来自最近会话接口，只有最后一条消息。不要转人工，礼貌请顾客复述或补充具体需求。",
        )
        intent["reply"] = final_reply
        self._store_intent_event(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            intent=intent,
            knowledge_hits=[],
        )

    @staticmethod
    def _append_latest_conversation_notice(business_context: str) -> str:
        notice = {
            "source": "latest_conversations",
            "instruction": "该消息来自上线时拉取的最近会话接口，接口通常只能提供最后一条消息。如果仅凭这条消息无法判断顾客需求，请礼貌请顾客复述或补充具体需求，不要猜测上下文，不要直接转人工。",
        }
        if not business_context:
            return json.dumps(
                {
                    "schema": "openkefu.latest_conversations_context.v1",
                    "latest_conversations_notice": notice,
                },
                ensure_ascii=False,
                default=str,
            )
        try:
            data = json.loads(business_context)
        except (TypeError, ValueError):
            data = {"raw_context": business_context}
        if isinstance(data, dict):
            data["latest_conversations_notice"] = notice
            return json.dumps(data, ensure_ascii=False, default=str)
        return json.dumps(
            {
                "raw_context": data,
                "latest_conversations_notice": notice,
            },
            ensure_ascii=False,
            default=str,
        )

    @staticmethod
    def _compact_product_source_context(product_context: dict[str, Any]) -> dict[str, Any]:
        amount = product_context.get("total_amount")
        price = ""
        try:
            if amount not in (None, ""):
                price = f"¥{float(amount) / 100:.2f}"
        except (TypeError, ValueError):
            price = str(amount)
        return {
            "source": "user_source_message",
            "title": product_context.get("title") or "",
            "goods_id": product_context.get("goods_id") or "",
            "goods_name": product_context.get("goods_name") or "",
            "goods_thumb_url": product_context.get("goods_thumb_url") or "",
            "mall_link_url": product_context.get("mall_link_url") or "",
            "display_price": price,
        }

    def _recent_product_cards(self, conversation_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT id, raw_json, message_at, created_at
            FROM messages
            WHERE conversation_id=%s AND raw_json IS NOT NULL
            ORDER BY id DESC
            LIMIT 80
            """,
            (conversation_id,),
        )
        items: list[dict[str, Any]] = []
        current = self.latest_product_context.get(int(conversation_id))
        if current:
            items.append(self._product_card_from_context(current, source="current"))

        for row in rows:
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                continue
            product_context = self._extract_product_context(raw)
            if not product_context:
                continue
            card = self._product_card_from_context(product_context, source="history")
            card["message_id"] = row.get("id")
            card["message_at"] = row.get("message_at") or row.get("created_at")
            items.append(card)

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            goods_id = str(item.get("goods_id") or "")
            if not goods_id or goods_id in seen:
                continue
            seen.add(goods_id)
            deduped.append(item)
            if len(deduped) >= 8:
                break
        return deduped

    @staticmethod
    def _product_card_from_context(product_context: dict[str, Any], *, source: str) -> dict[str, Any]:
        compact = ShopRunner._compact_product_source_context(product_context)
        return {
            "source": source,
            "goods_id": compact.get("goods_id") or "",
            "goods_name": compact.get("goods_name") or "",
            "goods_thumb_url": compact.get("goods_thumb_url") or "",
            "mall_link_url": compact.get("mall_link_url") or "",
            "display_price": compact.get("display_price") or "",
            "title": compact.get("title") or "",
        }

    def _goods_ids_for_reply(self, user_content: str, product_context: dict[str, Any] | None) -> list[int]:
        ids = GoodsService.extract_goods_ids(user_content)
        if product_context and product_context.get("goods_id"):
            try:
                ids.append(int(str(product_context["goods_id"])))
            except (TypeError, ValueError):
                pass
        return list(dict.fromkeys(ids))

    def _goods_context_for_llm(self, *, goods_id: int, conversation_id: int) -> dict[str, Any]:
        service = self._goods_service()
        if not service:
            return {
                "source": "goods_detail_api",
                "query": {"goods_id": goods_id},
                "status": "unavailable",
                "raw_response": None,
                "instruction": "商品详情接口不可用。不得编造商品名称、价格、库存、规格或上架状态；可告知顾客暂时无法核实。",
            }
        try:
            product = service.get_product_detail(goods_id)
            if not self._has_product_detail(product):
                return {
                    "source": "goods_detail_api",
                    "query": {"goods_id": goods_id},
                    "status": "not_found",
                    "raw_response": product,
                    "instruction": "商品详情接口返回为空或表示商品不存在。必须如实告知顾客未查询到该商品，不得编造商品名称、价格、库存、规格或上架状态。",
                }
            return {
                "source": "goods_detail_api",
                "query": {"goods_id": goods_id},
                "status": "found",
                "raw_response": product,
                "instruction": "请直接依据 raw_response 回答商品价格、库存、规格、上架状态、商品属性等问题；raw_response 没有的字段不得编造。",
            }
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "context.goods",
                "failed to fetch goods detail for llm context",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                error=exc,
                context={"goods_id": goods_id},
            )
            if self._is_not_found_error(exc):
                return {
                    "source": "goods_detail_api",
                    "query": {"goods_id": goods_id},
                    "status": "not_found",
                    "error": str(exc),
                    "raw_response": None,
                    "instruction": "商品详情接口明确返回不存在或未找到。必须如实告知顾客未查询到该商品，不得编造商品名称、价格、库存、规格或上架状态。",
                }
            return {
                "source": "goods_detail_api",
                "query": {"goods_id": goods_id},
                "status": "failed",
                "error": str(exc),
                "raw_response": None,
                "instruction": "商品详情接口查询失败。不得编造商品详情；可结合当前浏览商品基础信息礼貌说明暂时无法核实。",
            }

    def _order_context_for_llm(self, *, user_uid: str, conversation_id: int) -> dict[str, Any]:
        service = self._order_service()
        if not service:
            return {
                "source": "user_orders_api",
                "query": {"uid": user_uid, "page_no": 1, "page_size": 10},
                "status": "unavailable",
                "raw_response": None,
                "instruction": "订单接口不可用。不得编造订单号、订单状态、物流、退款或金额；可告知顾客暂时无法核实。",
            }
        try:
            orders = service.get_user_orders(user_uid, page_no=1, page_size=10)
            if not self._has_order_records(orders):
                return {
                    "source": "user_orders_api",
                    "query": {"uid": user_uid, "page_no": 1, "page_size": 10},
                    "status": "not_found",
                    "raw_response": orders,
                    "instruction": "订单接口返回空列表或表示订单不存在。必须如实告知顾客未查询到订单，不得编造订单号、订单状态、物流、退款或金额。",
                }
            return {
                "source": "user_orders_api",
                "query": {"uid": user_uid, "page_no": 1, "page_size": 10},
                "status": "found",
                "raw_response": orders,
                "instruction": "请直接依据 raw_response 回答订单状态、物流、售后、退款、金额等问题；raw_response 没有的字段不得编造。",
            }
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "context.orders",
                "failed to fetch order data for llm context",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                error=exc,
            )
            if self._is_not_found_error(exc):
                return {
                    "source": "user_orders_api",
                    "query": {"uid": user_uid, "page_no": 1, "page_size": 10},
                    "status": "not_found",
                    "error": str(exc),
                    "raw_response": None,
                    "instruction": "订单接口明确返回不存在或未找到。必须如实告知顾客未查询到订单，不得编造订单号、订单状态、物流、退款或金额。",
                }
            return {
                "source": "user_orders_api",
                "query": {"uid": user_uid, "page_no": 1, "page_size": 10},
                "status": "failed",
                "error": str(exc),
                "raw_response": None,
                "instruction": "订单接口查询失败。不得编造订单状态、物流、退款或金额；可请顾客稍等或提供订单信息后人工核实。",
            }

    def _lookup_order_snapshot(self, *, user_uid: str, order_no: str) -> dict[str, Any]:
        service = self._order_service()
        if not service:
            return {
                "status": "unavailable",
                "order_status": "待核实",
                "instruction": "订单接口不可用，记录已保留，后续需人工核实订单状态。",
            }
        query_order_no = str(order_no or "").strip()
        try:
            orders = service.get_user_orders(user_uid, page_no=1, page_size=10)
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "return_record.order_lookup",
                "failed to fetch order data for return record",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                user_uid=user_uid,
                error=exc,
                context={"order_no": query_order_no},
            )
            return {"status": "failed", "order_status": "查询失败", "error": str(exc)}

        order_list = self._order_list(orders)
        if query_order_no:
            order = self._find_order_by_no(orders, query_order_no)
        else:
            if len(order_list) > 1:
                return {
                    "status": "ambiguous",
                    "order_status": "待确认订单",
                    "query": {"order_no": query_order_no, "uid": user_uid},
                    "orders": order_list[:10],
                    "raw_response": orders,
                    "instruction": "该用户有多个订单，必须先请顾客确认订单号或商品名称，不得默认选择第一单。",
                }
            order = order_list[0] if order_list else None
        if not order:
            return {
                "status": "not_found",
                "order_status": "未查询到订单",
                "query": {"order_no": query_order_no, "uid": user_uid},
                "raw_response": orders,
            }
        raw_status = order.get("order_status") or order.get("orderStatus") or order.get("status") or ""
        resolved_order_no = (
            order.get("order_sn")
            or order.get("orderSn")
            or order.get("order_no")
            or order.get("orderNo")
            or order.get("order_id")
            or order.get("orderId")
            or query_order_no
        )
        return {
            "status": "found",
            "order_status": self._order_status_text(raw_status),
            "order_no": str(resolved_order_no or ""),
            "query": {"order_no": query_order_no, "uid": user_uid},
            "order": order,
        }

    @staticmethod
    def _order_list(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        if not isinstance(order_list, list):
            return []
        return [order for order in order_list if isinstance(order, dict)]

    @staticmethod
    def _find_order_by_no(data: Any, order_no: str) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        if not isinstance(order_list, list):
            return None
        expected = str(order_no or "").strip()
        for order in order_list:
            if not isinstance(order, dict):
                continue
            candidates = (
                order.get("order_sn"),
                order.get("orderSn"),
                order.get("order_no"),
                order.get("orderNo"),
                order.get("order_id"),
                order.get("orderId"),
            )
            if any(str(value or "").strip() == expected for value in candidates):
                return order
        return None

    @staticmethod
    def _first_order(data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        if not isinstance(order_list, list):
            return None
        return next((order for order in order_list if isinstance(order, dict)), None)

    @staticmethod
    def _has_product_detail(product: Any) -> bool:
        if not isinstance(product, dict) or not product:
            return False
        useful_keys = (
            "goods_id", "goodsId", "goods_name", "goodsName", "goods_sn",
            "goods_desc", "share_desc", "skus", "status", "mall_name",
        )
        return any(product.get(key) not in (None, "", [], {}) for key in useful_keys)

    @staticmethod
    def _has_order_records(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        return isinstance(order_list, list) and len(order_list) > 0

    @classmethod
    def _order_cards(cls, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        if not isinstance(order_list, list):
            return []
        cards: list[dict[str, Any]] = []
        for order in order_list[:10]:
            if not isinstance(order, dict):
                continue
            goods_list = order.get("goods_list") or order.get("goodsList") or order.get("items") or []
            goods_cards = []
            if isinstance(goods_list, list):
                for goods in goods_list[:3]:
                    if not isinstance(goods, dict):
                        continue
                    goods_cards.append(
                        {
                            "goods_name": goods.get("goods_name") or goods.get("goodsName") or goods.get("name") or "",
                            "spec": goods.get("spec") or goods.get("goods_spec") or "",
                            "quantity": goods.get("count") or goods.get("quantity") or goods.get("goods_number") or "",
                            "thumb_url": goods.get("thumb_url") or goods.get("goods_thumb_url") or goods.get("image") or "",
                        }
                    )
            amount = order.get("amount") or order.get("order_amount") or order.get("pay_amount") or order.get("orderAmount") or 0
            cards.append(
                {
                    "order_sn": order.get("order_sn") or order.get("orderSn") or "",
                    "status": cls._order_status_text(order.get("order_status") or order.get("orderStatus") or order.get("status") or ""),
                    "amount": cls._money_text(amount),
                    "created_at": order.get("created_at") or order.get("createdAt") or order.get("order_time") or "",
                    "after_sale_status": order.get("after_sale_status") or order.get("afterSaleStatus") or "",
                    "shipping_status": order.get("shipping_status") or order.get("shippingStatus") or order.get("tracking_status") or "",
                    "goods": goods_cards,
                }
            )
        return cards

    @staticmethod
    def _money_text(value: Any) -> str:
        if value in (None, "", 0):
            return ""
        try:
            return f"¥{float(value) / 100:.2f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _order_status_text(status: Any) -> str:
        text = str(status or "")
        labels = {
            "0": "待付款",
            "1": "待发货",
            "2": "已发货",
            "3": "已签收",
            "4": "已取消",
            "5": "已完成",
            "6": "退款中",
            "7": "已退款",
            "unpaid": "待付款",
            "paid": "待发货",
            "shipped": "已发货",
            "completed": "已完成",
            "cancelled": "已取消",
            "refund": "退款中",
        }
        return labels.get(text.lower(), text)

    @staticmethod
    def _is_not_found_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "不存在", "未找到", "没找到", "没有找到", "无记录", "暂无记录",
                "not found", "not exist", "not exists", "no record", "empty",
            )
        )

    def _goods_service(self) -> GoodsService | None:
        if self.goods_service:
            return self.goods_service
        if not self.login:
            return None
        self.goods_service = GoodsService(self.login)
        return self.goods_service

    def _order_service(self) -> OrderService | None:
        if self.order_service:
            return self.order_service
        if not self.login:
            return None
        self.order_service = OrderService(self.login)
        return self.order_service

    @staticmethod
    def _looks_like_order_question(text: str) -> bool:
        normalized = str(text or "")
        return any(
            keyword in normalized
            for keyword in (
                "订单", "物流", "快递", "发货了吗", "发货没", "什么时候发货", "到哪",
                "退款", "退货", "换货", "售后", "签收", "没收到", "改地址", "地址",
                "催发货", "运单", "单号",
            )
        )

    @staticmethod
    def _looks_like_product_question(text: str, *, has_product_context: bool) -> bool:
        normalized = str(text or "")
        product_keywords = (
            "商品", "这款", "这个", "链接", "价格", "多少钱", "库存", "有货",
            "规格", "型号", "尺寸", "颜色", "材质", "参数", "质量", "保修",
            "包邮", "发什么", "适用", "支持", "能用", "怎么用", "静音", "有线",
        )
        if any(keyword in normalized for keyword in product_keywords):
            return True
        return has_product_context and len(normalized.strip()) <= 30

    def _optimize_qa_reply(self, reply: str) -> tuple[str, int | None, bool]:
        messages = [
            {
                "role": "system",
                "content": "你是拼多多店铺客服文案优化助手。请在保持原意、关键信息和承诺不变的前提下，将回复优化得更自然、亲切、简洁。只输出优化后的回复，不要解释。",
            },
            {"role": "user", "content": reply},
        ]
        quota_owner, quota_exhausted = self._reserve_creator_llm_quota("大模型回复次数已达上限，所有店铺已下线")
        try:
            return clean_reply_text(self.llm_client.chat(messages, max_tokens=512, temperature=0.3)), quota_owner, quota_exhausted
        except Exception:
            return reply, quota_owner, quota_exhausted

    def _send_cached_or_fast_reply(self, conversation_id: int, message_id: int, user_uid: str,
                                    reply: str, *, source: str, intent: dict[str, Any] | None = None):
        if intent:
            intent_event_id = self._store_intent_event(
                conversation_id=conversation_id,
                message_id=message_id,
                user_uid=user_uid,
                intent=intent,
                knowledge_hits=[],
            )
            self.actions.create_and_dispatch(
                shop_id=self.shop_id,
                conversation_id=conversation_id,
                message_id=message_id,
                intent_event_id=intent_event_id,
                user_uid=user_uid,
                actions=intent.get("actions") or [],
                slots=intent.get("slots") or {},
            )
            if self._intent_needs_human(intent):
                if not self._is_force_ai_reply():
                    self._handle_need_human(conversation_id, user_uid, "transfer_to_human")
                    return
        self._send_auto_text_reply(
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            content=reply,
            context_instruction=f"原回复来自{source}，重写时保持原意，简洁自然地回答顾客。",
        )
        self.runtime_logger.log(
            "INFO",
            __name__,
            f"reply.{source}",
            f"reply via {source}",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
        )

    def _send_auto_text_reply(
        self,
        *,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        content: str,
        history: list[dict[str, str]] | None = None,
        knowledge_hits: list[dict[str, Any]] | None = None,
        shop_notes: list[str] | None = None,
        business_context: str | None = None,
        context_instruction: str | None = None,
    ) -> str:
        content = clean_reply_text(content)
        # 发送失败不再重新生成文案重发：send_reply 内部已用同一个
        # client_msg_id 做幂等重试（服务端按 client_msg_id 去重）。
        # 这里直接抛出，避免"请求超时但实际已送达"时顾客收到两条不同回复。
        self.send_reply(
            conversation_id=conversation_id,
            content=content,
            source_message_id=message_id,
            auto=True,
            send_attempts=2,
        )
        return content

    @staticmethod
    def _intent_needs_human(intent: dict[str, Any]) -> bool:
        actions = intent.get("actions") or []
        return (
            any(isinstance(item, dict) and item.get("type") == "transfer_to_human" for item in actions)
            or intent.get("resolution_status") == "need_human"
        )

    @staticmethod
    def _is_cacheable(query: str, intent: dict[str, Any]) -> bool:
        confidence = float(intent.get("confidence") or 0)
        resolution = intent.get("resolution_status") or ""
        if ShopRunner._intent_needs_human(intent):
            return False
        if confidence < 0.75:
            return False
        if resolution in ("need_human", "need_more_info"):
            return False
        if intent.get("_cache_hit"):
            return False
        return len(query) >= 8

    def _suppress_unwanted_transfer(self, user_content: str, intent: dict[str, Any], conversation_id: int) -> dict[str, Any]:
        actions = [dict(item) for item in (intent.get("actions") or []) if isinstance(item, dict)]
        transfer_actions = [item for item in actions if item.get("type") == "transfer_to_human"]
        if not transfer_actions:
            return intent
        if self._customer_requested_human(user_content) or intent.get("error"):
            return intent

        sanitized = dict(intent)
        sanitized["actions"] = [item for item in actions if item.get("type") != "transfer_to_human"]
        if sanitized.get("resolution_status") == "need_human":
            sanitized["resolution_status"] = "need_more_info"
        reply = clean_reply_text(str(sanitized.get("reply") or ""))
        if not reply or self._reply_only_points_to_human(reply):
            reply = self._non_transfer_fallback_reply()
        sanitized["reply"] = reply
        sanitized["_transfer_suppressed"] = True
        self.runtime_logger.log(
            "INFO",
            __name__,
            "reply.transfer_suppressed",
            "suppressed transfer_to_human because customer did not explicitly request human service",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            conversation_id=conversation_id,
            context={
                "intent_code": sanitized.get("intent_code"),
                "confidence": sanitized.get("confidence"),
                "resolution_status": intent.get("resolution_status"),
                "suppressed_actions": transfer_actions,
            },
        )
        return sanitized

    @staticmethod
    def _customer_requested_human(text: str) -> bool:
        normalized = str(text or "").lower()
        keywords = (
            "转人工",
            "人工客服",
            "真人客服",
            "找人工",
            "找真人",
            "人工处理",
            "人工",
            "真人",
            "投诉",
            "举报",
            "平台介入",
            "不要机器人",
            "别机器人",
        )
        return any(keyword in normalized for keyword in keywords)

    @staticmethod
    def _reply_only_points_to_human(reply: str) -> bool:
        text = str(reply or "")
        transfer_keywords = ("转人工", "人工客服", "人工处理", "安排客服", "安排人工", "稍等", "转接")
        return any(keyword in text for keyword in transfer_keywords)

    @staticmethod
    def _non_transfer_fallback_reply() -> str:
        return "亲，当前信息还不够完整，请您补充一下订单号、商品名称或具体问题，我继续帮您处理。"

    @staticmethod
    def _knowledge_query(history: list[dict[str, str]]) -> str:
        user_items = [item["content"] for item in history if item.get("role") == "user" and item.get("content")]
        return "\n".join(user_items[-3:])[:3000]

    def _store_intent_event(
        self,
        *,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        intent: dict[str, Any],
        knowledge_hits: list[dict[str, Any]],
    ) -> int:
        intent_event_id = self.db.execute(
            """
            INSERT INTO intent_events
            (shop_id, conversation_id, message_id, user_uid, intent_code, raw_intent_code,
             confidence, resolution_status, reply, slots_json, actions_json, knowledge_json,
             raw_json, status, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'replied',%s)
            """,
            (
                self.shop_id,
                conversation_id,
                message_id,
                user_uid,
                intent.get("intent_code") or "unknown",
                intent.get("raw_intent_code") or intent.get("intent_code") or "unknown",
                float(intent.get("confidence") or 0),
                intent.get("resolution_status") or "need_human",
                intent.get("reply") or "",
                json_dumps(intent.get("slots") or {}),
                json_dumps(intent.get("actions") or []),
                json_dumps(knowledge_hits or []),
                json_dumps(intent.get("raw") or intent),
                intent.get("error") or None,
            ),
        )
        self.hub.publish({"type": "intent_event", "data": self.db.query_one("SELECT * FROM intent_events WHERE id=%s", (intent_event_id,))})
        return intent_event_id

    def _llm_history(self, *, conversation_id: int, current_message_id: int) -> list[dict[str, str]]:
        sort_expr = (
            "CASE "
            "WHEN message_at IS NULL THEN UNIX_TIMESTAMP(created_at) * 1000 "
            "WHEN message_at < 100000000000 THEN message_at * 1000 "
            "ELSE message_at END"
        )
        current = self.db.query_one(
            f"SELECT {sort_expr} AS sort_at FROM messages WHERE id=%s",
            (current_message_id,),
        )
        if not current:
            return []
        cutoff = int(current.get("sort_at") or 0)
        boundary_message_id = self._latest_transfer_boundary_message_id(conversation_id, current_message_id)
        rows = self.db.query(
            f"""
            SELECT direction, kind, content, goods_json, raw_json
            FROM messages
            WHERE conversation_id=%s
              AND id>%s
              AND direction IN ('inbound','outbound')
              AND (
                {sort_expr} < %s
                OR ({sort_expr} = %s AND id <= %s)
              )
            ORDER BY {sort_expr} DESC, id DESC
            LIMIT 11
            """,
            (conversation_id, boundary_message_id, cutoff, cutoff, current_message_id),
        )
        history: list[dict[str, str]] = []
        for row in reversed(rows):
            role = "assistant" if row.get("direction") == "outbound" else "user"
            content = self._message_content_for_llm(row)
            if content:
                history.append({"role": role, "content": content})
        return history

    def _latest_transfer_boundary_message_id(self, conversation_id: int, current_message_id: int) -> int:
        row = self.db.query_one(
            """
            SELECT ie.message_id AS source_message_id,
                   (
                     SELECT MIN(m.id)
                     FROM messages m
                     WHERE m.conversation_id=ie.conversation_id
                       AND m.id>ie.message_id
                       AND m.direction='outbound'
                   ) AS transfer_reply_message_id
            FROM intent_events ie
            WHERE ie.conversation_id=%s
              AND ie.message_id<%s
              AND ie.actions_json LIKE %s
            ORDER BY ie.id DESC
            LIMIT 1
            """,
            (conversation_id, current_message_id, "%transfer_to_human%"),
        )
        if not row:
            return 0
        return int(row.get("transfer_reply_message_id") or row.get("source_message_id") or 0)

    @staticmethod
    def _message_content_for_llm(row: dict[str, Any]) -> str:
        kind = row.get("kind") or "text"
        content = str(row.get("content") or "").strip()
        if kind == "goods" and row.get("goods_json"):
            try:
                goods = json.loads(row["goods_json"])
            except (TypeError, ValueError):
                goods = {}
            name = goods.get("name") or "商品"
            price = goods.get("price") or ""
            goods_id = goods.get("id") or ""
            link = goods.get("link") or content
            parts = [f"商品名称：{name}"]
            if goods_id:
                parts.append(f"商品ID：{goods_id}")
            if price:
                parts.append(f"价格：{price}")
            if link:
                parts.append(f"链接：{link}")
            return "[商品卡片] " + "；".join(parts)
        if kind == "image":
            return f"[图片消息] {content or '顾客发送了一张图片，请结合上下文判断并必要时追问图片内容。'}"
        if kind == "link":
            return f"[链接消息] {content or '顾客发送了一个链接。'}"
        if kind == "system":
            return f"[系统消息] {content}" if content else ""
        if kind == "unknown":
            raw_summary = ShopRunner._raw_message_summary(row.get("raw_json"))
            return f"[未知类型消息] {content or raw_summary}"
        if content:
            return f"[文本消息] {content}" if kind == "text" else f"[{kind}消息] {content}"
        raw_summary = ShopRunner._raw_message_summary(row.get("raw_json"))
        return f"[{kind}消息] {raw_summary}" if raw_summary else ""

    @staticmethod
    def _raw_message_summary(raw_json: Any) -> str:
        if not raw_json:
            return ""
        try:
            raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        except (TypeError, ValueError):
            return str(raw_json)[:300]
        if not isinstance(raw, dict):
            return str(raw)[:300]
        summary: dict[str, Any] = {}
        for key in ("type", "msg_id", "content", "decoded_content", "info", "size"):
            value = raw.get(key)
            if value not in (None, "", {}, []):
                summary[key] = value
        return json.dumps(summary or raw, ensure_ascii=False, default=str)[:500]

    def send_image(self, *, conversation_id: int, image_base64: str):
        if not self.customer_service or not self.token_result:
            raise RuntimeError("shop is not online")
        self._load_sendable_conversation(conversation_id)
        if self._is_conversation_transferred(conversation_id):
            raise RuntimeError("会话已转接，无法发送消息，请等待用户回复后重试")

        conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise ValueError(f"conversation not found: {conversation_id}")
        user_uid = str(conversation["user_uid"])

        attempt_id = self.db.execute(
            """
            INSERT INTO reply_attempts
            (shop_id, conversation_id, message_id, user_uid, content, status)
            VALUES (%s,%s,%s,%s,%s,'sending')
            """,
            (self.shop_id, conversation_id, None, user_uid, "[图片]"),
        )
        message_id = self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, user_uid, sender_role, kind, content, status)
            VALUES (%s,%s,'outbound',%s,'service','image','[图片]','sending')
            """,
            (self.shop_id, conversation_id, user_uid),
        )
        self.hub.publish({"type": "message", "data": self._message_row(message_id)})

        try:
            for attempt in range(2):
                client, token_result, mall_id = self._send_session_snapshot()
                try:
                    result, image_url = client.send_image_message(
                        user_uid, image_base64, token_result=token_result,
                        mall_id=mall_id, chat_type=conversation.get("chat_type") or None,
                    )
                    if not self._send_message_response_ok(result):
                        raise RuntimeError(f"send_message response invalid: {self._send_message_failure_text(result)}")
                    break
                except Exception as exc:
                    # Retry only an explicit rejection; an image send timeout may
                    # already have delivered and has no stable client_msg_id here.
                    if attempt or not self._send_session_expired(exc):
                        raise
                    self._recover_send_session(client)
        except Exception as exc:
            self.db.execute("UPDATE reply_attempts SET status='failed', error=%s WHERE id=%s", (str(exc), attempt_id))
            self.db.execute("UPDATE messages SET status='failed', error=%s WHERE id=%s", (str(exc), message_id))
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "reply.image",
                "image send failed",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                request_id=attempt_id,
                error=exc,
            )
            self.hub.publish({"type": "reply_result", "data": {"id": attempt_id, "shop_id": self.shop_id, "status": "failed", "error": "图片发送失败"}})
            self.hub.publish({"type": "message", "data": self._message_row(message_id)})
            raise

        self._persist_login_cache_after_send()
        self.db.execute(
            "UPDATE reply_attempts SET status='success', result_json=%s, content=%s WHERE id=%s",
            (json_dumps(result), image_url, attempt_id),
        )
        self.db.execute(
            "UPDATE messages SET status='sent', content=%s, raw_json=%s WHERE id=%s",
            (image_url, json_dumps(result), message_id),
        )
        self.hub.publish({"type": "reply_result", "data": {"id": attempt_id, "shop_id": self.shop_id, "status": "success", "result": result}})
        self.hub.publish({"type": "message", "data": self._message_row(message_id)})
        return result

    @staticmethod
    def _send_message_response_ok(result: Any) -> bool:
        return send_message_response_ok(result)

    @staticmethod
    def _send_message_failure_text(result: Any) -> str:
        return send_message_failure_text(result)

    @staticmethod
    def _send_session_expired(error: Any) -> bool:
        return is_session_expired(error)

    def _chat_type_id_for_send(self, conversation: dict[str, Any], source_message_id: int | None) -> Any:
        if source_message_id:
            source = self.db.query_one(
                "SELECT raw_json FROM messages WHERE id=%s AND shop_id=%s",
                (source_message_id, self.shop_id),
            )
            try:
                raw = json.loads((source or {}).get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            if isinstance(raw, dict) and raw.get("_source") == "latest_conversations":
                return conversation.get("chat_type_id") or None
        return conversation.get("chat_type_id") or 9

    def _message_fields_for_send(self, conversation: dict[str, Any], source_message_id: int | None) -> dict[str, Any] | None:
        if not source_message_id:
            return None
        source = self.db.query_one(
            "SELECT raw_json FROM messages WHERE id=%s AND shop_id=%s",
            (source_message_id, self.shop_id),
        )
        try:
            raw = json.loads((source or {}).get("raw_json") or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("_source") != "latest_conversations":
            return None

        source_from = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        source_to = raw.get("to") if isinstance(raw.get("to"), dict) else {}
        fields: dict[str, Any] = {
            "to": {
                "role": "user",
                "uid": str(conversation.get("user_uid") or source_from.get("uid") or ""),
            },
        }
        if source_to:
            fields["from"] = {
                key: value
                for key, value in source_to.items()
                if value not in (None, "")
            }
            fields["from"].setdefault("role", "mall_cs")
        pre_msg_id = raw.get("msg_id") or raw.get("client_msg_id")
        if pre_msg_id not in (None, ""):
            fields["pre_msg_id"] = str(pre_msg_id)
        return fields

    def send_reply(
        self,
        *,
        conversation_id: int,
        content: str,
        source_message_id: int | None = None,
        auto: bool = False,
        send_attempts: int = 2,
    ):
        if not self.customer_service or not self.token_result:
            raise RuntimeError("shop is not online")
        self._load_sendable_conversation(conversation_id)
        if self._is_conversation_transferred(conversation_id):
            raise RuntimeError("会话已转接，无法发送消息，请等待用户回复后重试")
        content = clean_reply_text(content)
        conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise ValueError(f"conversation not found: {conversation_id}")
        user_uid = str(conversation["user_uid"])
        attempt_id = self.db.execute(
            """
            INSERT INTO reply_attempts
            (shop_id, conversation_id, message_id, user_uid, content, status)
            VALUES (%s,%s,%s,%s,%s,'sending')
            """,
            (self.shop_id, conversation_id, source_message_id, user_uid, content),
        )
        message_id = self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, user_uid, sender_role, kind, content, status)
            VALUES (%s,%s,'outbound',%s,'service','text',%s,'sending')
            """,
            (self.shop_id, conversation_id, user_uid, content),
        )
        self.hub.publish({"type": "message", "data": self._message_row(message_id)})
        max_attempts = max(1, int(send_attempts or 1))
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        sent_attempt = 0
        send_chat_type_id = self._chat_type_id_for_send(conversation, source_message_id)
        send_message_fields = self._message_fields_for_send(conversation, source_message_id)
        # 幂等键：整个重试循环复用同一个 client_msg_id，
        # 服务端按 client_msg_id 去重，避免"请求超时但已送达"时顾客收到重复消息
        send_client_msg_id = (
            self.customer_service.build_client_msg_id(conversation.get("conv_id") or str(user_uid))
        )

        for attempt in range(1, max_attempts + 1):
            sent_attempt = attempt
            try:
                client, token_result, mall_id = self._send_session_snapshot()
                result = client.send_text_message(
                    user_uid,
                    content,
                    token_result=token_result,
                    mall_id=mall_id,
                    conv_id=conversation.get("conv_id"),
                    chat_type_id=send_chat_type_id,
                    chat_type=conversation.get("chat_type") or None,
                    message_fields=send_message_fields,
                    client_msg_id=send_client_msg_id,
                )
                if not self._send_message_response_ok(result):
                    raise RuntimeError(f"send_message response invalid: {self._send_message_failure_text(result)}")
                break
            except Exception as exc:
                last_error = exc
                if self._send_session_expired(exc) and attempt < max_attempts:
                    self.runtime_logger.log(
                        "WARNING",
                        __name__,
                        "reply.session_expired",
                        "reply send session expired, reconnecting before retry",
                        shop_id=self.shop_id,
                        mall_id=self.mall_id,
                        conversation_id=conversation_id,
                        user_uid=user_uid,
                        request_id=attempt_id,
                        error=exc,
                        context={"attempt": attempt, "max_attempts": max_attempts, "auto": auto},
                    )
                    try:
                        self._recover_send_session(client)
                    except Exception as reconnect_exc:
                        last_error = reconnect_exc
                        result = None
                        break
                    conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
                    if not conversation:
                        last_error = ValueError(f"conversation not found: {conversation_id}")
                        result = None
                        break
                    send_chat_type_id = self._chat_type_id_for_send(conversation, source_message_id)
                    send_message_fields = self._message_fields_for_send(conversation, source_message_id)
                    continue
                # Only the service API carries the stable client_msg_id. The
                # legacy/chat_type path cannot safely retry unknown delivery.
                retry_with_same_id = (
                    not conversation.get("chat_type")
                    and CustomerServiceClient._is_service_chat_type(send_chat_type_id)
                )
                if attempt >= max_attempts or not retry_with_same_id:
                    result = None
                    break
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "reply.retry",
                    "reply send failed, retrying once",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                    conversation_id=conversation_id,
                    user_uid=user_uid,
                    request_id=attempt_id,
                    error=exc,
                    context={"attempt": attempt, "max_attempts": max_attempts, "auto": auto},
                )
                time.sleep(0.3)

        if result is None:
            exc = last_error or RuntimeError("send_message failed")
            self.db.execute("UPDATE reply_attempts SET status='failed', error=%s WHERE id=%s", (str(exc), attempt_id))
            self.db.execute("UPDATE messages SET status='failed', error=%s WHERE id=%s", (str(exc), message_id))
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "reply.auto" if auto else "reply.manual",
                "reply failed",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                request_id=attempt_id,
                error=exc,
                context={"send_attempts": sent_attempt},
            )
            self.hub.publish({"type": "reply_result", "data": {"id": attempt_id, "shop_id": self.shop_id, "status": "failed", "error": str(exc)}})
            self.hub.publish({"type": "message", "data": self._message_row(message_id)})
            raise exc

        self._persist_login_cache_after_send()
        if isinstance(result, dict):
            result = dict(result)
            result["_send_attempts"] = sent_attempt
        self.db.execute(
            "UPDATE reply_attempts SET status='success', result_json=%s WHERE id=%s",
            (json_dumps(result), attempt_id),
        )
        self.db.execute(
            "UPDATE messages SET status='sent', raw_json=%s WHERE id=%s",
            (json_dumps(result), message_id),
        )
        self.hub.publish({"type": "reply_result", "data": {"id": attempt_id, "shop_id": self.shop_id, "status": "success", "result": result}})
        self.hub.publish({"type": "message", "data": self._message_row(message_id)})
        return result

    def _shop_creator_id(self) -> int | None:
        shop = self.db.query_one("SELECT created_by_user_id, created_by FROM shops WHERE id=%s", (self.shop_id,))
        if not shop:
            return None
        creator = shop.get("created_by_user_id")
        if creator:
            return int(creator)
        assignments = self.db.query("SELECT user_id FROM shop_assignments WHERE shop_id=%s ORDER BY id LIMIT 2", (self.shop_id,))
        if len(assignments) == 1:
            return int(assignments[0]["user_id"])
        creator = shop.get("created_by")
        return int(creator) if creator else None

    def _require_creator_llm_quota(self, error_message: str) -> int | None:
        creator = self._shop_creator_id()
        if not creator:
            raise RuntimeError("无法确认店铺额度归属，禁止操作店铺")
        user_row = self.db.query_one("SELECT role, max_llm_replies, llm_reply_count FROM users WHERE id=%s", (creator,))
        if not user_row:
            raise RuntimeError("店铺额度归属用户不存在，禁止操作店铺")
        # 管理员不受额度限制
        if str(user_row.get("role") or "") == "admin":
            return creator
        max_replies = int(user_row.get("max_llm_replies") or 0)
        used = int(user_row.get("llm_reply_count") or 0)
        if used >= max_replies:
            self._quota_exceeded(creator)
            raise RuntimeError(error_message)
        return creator

    def _reserve_creator_llm_quota(self, error_message: str) -> tuple[int | None, bool]:
        creator = self._shop_creator_id()
        if not creator:
            raise RuntimeError("无法确认店铺额度归属，禁止调用大模型")
        user_row = self.db.query_one("SELECT role, max_llm_replies, llm_reply_count FROM users WHERE id=%s", (creator,))
        if not user_row:
            raise RuntimeError("店铺额度归属用户不存在，禁止调用大模型")
        if str(user_row.get("role") or "") == "admin":
            # 管理员不受额度限制：只计数不拦截
            self.db.execute("UPDATE users SET llm_reply_count = llm_reply_count + 1 WHERE id=%s", (creator,))
            return creator, False
        affected = self.db.execute(
            """
            UPDATE users
            SET llm_reply_count = llm_reply_count + 1
            WHERE id=%s AND llm_reply_count < max_llm_replies
            """,
            (creator,),
        )
        if affected < 1:
            self._quota_exceeded(creator)
            raise RuntimeError(error_message)
        max_replies = int(user_row.get("max_llm_replies") or 0)
        used = int(user_row.get("llm_reply_count") or 0) + 1
        return creator, used >= max_replies

    def _quota_exceeded(self, user_id: int) -> None:
        shops = self.db.query(
            """
            SELECT DISTINCT s.id
            FROM shops s
            LEFT JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE (s.created_by_user_id=%s OR sa.user_id=%s OR s.created_by=%s)
              AND s.status IN ('online','connecting','logged_in','login_success','qr_scanned','login_pending','qr_pending')
            """,
            (user_id, user_id, user_id),
        )
        for s in shops:
            shop_id = s["id"]
            if shop_id == self.shop_id:
                continue
            try:
                if self.manager:
                    self.manager.offline_shop(shop_id)
            except Exception:
                pass

        self._disconnect_customer_service(update_session=True)
        self._set_shop_status("offline")
        self.hub.publish({"type": "llm_quota_exceeded", "data": {"user_id": user_id}})
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def transfer(self, *, conversation_id: int, csid: str, remark: str):
        if not self.transfer_client:
            raise RuntimeError("shop is not online")
        self._load_sendable_conversation(conversation_id)
        conversation = self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise ValueError(f"conversation not found: {conversation_id}")
        user_uid = str(conversation["user_uid"])
        attempt_id = self.db.execute(
            """
            INSERT INTO transfer_attempts
            (shop_id, conversation_id, user_uid, csid, remark, status)
            VALUES (%s,%s,%s,%s,%s,'sending')
            """,
            (self.shop_id, conversation_id, user_uid, csid, remark or ""),
        )
        try:
            result = self.transfer_client.move_conversation(
                csid=csid,
                user_uid=user_uid,
                remark=remark,
            )
        except Exception as exc:
            self.db.execute("UPDATE transfer_attempts SET status='failed', error=%s WHERE id=%s", (str(exc), attempt_id))
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "conversation.transfer",
                "transfer failed",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                request_id=attempt_id,
                error=exc,
            )
            self.hub.publish({"type": "transfer_result", "data": {"id": attempt_id, "shop_id": self.shop_id, "status": "failed", "error": str(exc), "conversation_id": conversation_id}})
            raise

        self.db.execute(
            "UPDATE transfer_attempts SET status='success', result_json=%s WHERE id=%s",
            (json_dumps(result), attempt_id),
        )

        close_result = None
        try:
            close_result = self.transfer_client.close_conversation(user_uid=user_uid)
            self.runtime_logger.log(
                "INFO",
                __name__,
                "conversation.close_after_transfer",
                "conversation closed after transfer",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
            )
        except Exception as close_exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "conversation.close_after_transfer",
                "failed to close conversation after transfer",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                error=close_exc,
            )

        self.db.execute(
            """
            UPDATE conversations
            SET transferred_at=NOW(),
                bot_reply_enabled=0,
                human_attention_required=0,
                human_attention_reason=NULL,
                human_attention_at=NULL
            WHERE id=%s
            """,
            (conversation_id,),
        )
        conversation_row = self.db.query_one(
            """
            SELECT c.*, s.name AS shop_name, s.status AS shop_status,
                   s.auto_reply_enabled AS shop_auto_reply_enabled
            FROM conversations c
            JOIN shops s ON s.id=c.shop_id
            WHERE c.id=%s
            """,
            (conversation_id,),
        )
        if conversation_row:
            self.hub.publish({"type": "conversation", "data": conversation_row})

        self.hub.publish({
            "type": "transfer_result",
            "data": {
                "id": attempt_id,
                "shop_id": self.shop_id,
                "status": "success",
                "conversation_id": conversation_id,
                "user_uid": user_uid,
                "csid": csid,
                "result": result,
                "closed": close_result is not None,
            },
        })
        return result

    def get_assignable_services(self) -> list[dict[str, Any]]:
        if not self.transfer_client:
            raise RuntimeError("shop is not online")
        services = self.transfer_client.list_customer_services()
        return [s for s in services if str(s.get("csid") or "") != str(self.mall_id or "")]

    def _on_titan_error(self, error: Exception) -> None:
        self._set_error(error)
        if self._is_ws_token_fatal_error(error):
            self.runtime_logger.log(
                "ERROR",
                __name__,
                "ws.reconnect.abort",
                f"ws token/account error is fatal, stop auto reconnect: {error}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
            )
            self._cancel_ws_reconnect()
            return
        self._schedule_ws_reconnect()

    @staticmethod
    def _is_ws_token_fatal_error(error: Exception) -> bool:
        message = str(error)
        lower = message.lower()
        if isinstance(error, PermissionError):
            return True
        return any(
            keyword in lower
            for keyword in (
                "在别处登录",
                "relogin",
                "账号已下线",
                "被挤下线",
            )
        )

    def _schedule_ws_reconnect(self) -> None:
        with self.lock:
            if self._ws_reconnect_attempts >= self.MAX_WS_RECONNECT_ATTEMPTS:
                self.runtime_logger.log(
                    "ERROR",
                    __name__,
                    "ws.reconnect.give_up",
                    f"ws auto reconnect gave up after {self.MAX_WS_RECONNECT_ATTEMPTS} attempts; manual login required",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                )
                self._ws_reconnect_attempts = 0
                return
            self._ws_reconnect_attempts += 1
            delay = min(
                self.WS_RECONNECT_MAX_DELAY,
                self.WS_RECONNECT_BASE_DELAY * (2 ** (self._ws_reconnect_attempts - 1)),
            )
            generation = self._ws_reconnect_generation
            self._cancel_ws_reconnect_timer()
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "ws.reconnect.schedule",
                f"ws auto reconnect attempt {self._ws_reconnect_attempts}/{self.MAX_WS_RECONNECT_ATTEMPTS} in {delay:.0f}s",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
            )
            timer = threading.Timer(delay, self._ws_reconnect_tick, args=(generation,))
            timer.daemon = True
            self._ws_reconnect_timer = timer
            timer.start()

    def _ws_reconnect_tick(self, generation: int) -> None:
        with self.lock:
            # 锁内校验 generation 与 listener 状态：用户点击下线/停止会
            # 在锁内递增 generation 并置 listener=None，本 tick 与之互斥；
            # 若校验通过后重连执行期间用户才下线（generation 再变），
            # 由成功分支的回滚逻辑兜底
            if generation != self._ws_reconnect_generation:
                return
            if self._ws_reconnect_timer is not None:
                self._ws_reconnect_timer = None
            if self.listener is None or self.listener.get("close_requested"):
                return
        try:
            # 自动重连场景：内部 disconnect 不取消重连状态（generation 保持不变），
            # 使本 tick 的 generation 复核与失败后的再调度仍然有效
            self._reconnect_customer_service_from_cache(reason="auto-reconnect", cancel_reconnect=False)
            with self.lock:
                reconnect_still_valid = generation == self._ws_reconnect_generation
                if reconnect_still_valid:
                    self._ws_reconnect_attempts = 0
            if not reconnect_still_valid:
                # 重连执行期间用户点击了下线/停止（generation 已递增）：回滚刚建立
                # 的连接，保证用户的下线意图不被自动重连抵消
                self.runtime_logger.log(
                    "WARNING",
                    __name__,
                    "ws.reconnect.rolled_back",
                    "auto reconnect finished after user requested offline; rolling back",
                    shop_id=self.shop_id,
                    mall_id=self.mall_id,
                )
                self._disconnect_customer_service(update_session=True, session_status="offline")
                self._set_shop_status("offline")
                self.hub.publish({"type": "shop_status", "data": self._shop_row()})
                return
        except Exception as exc:
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "ws.reconnect.failed",
                f"ws auto reconnect attempt failed: {exc}",
                shop_id=self.shop_id,
                mall_id=self.mall_id,
                error=exc,
            )
            self._set_error(exc)
            with self.lock:
                if generation == self._ws_reconnect_generation:
                    self._schedule_ws_reconnect()
                else:
                    # 重连期间用户已下线/停止，不再调度
                    self._ws_reconnect_attempts = 0

    def _cancel_ws_reconnect_timer(self) -> None:
        timer = self._ws_reconnect_timer
        if timer is not None:
            timer.cancel()
            self._ws_reconnect_timer = None

    def _cancel_ws_reconnect(self) -> None:
        with self.lock:
            self._ws_reconnect_generation += 1
            self._cancel_ws_reconnect_timer()
            self._ws_reconnect_attempts = 0

    def _on_pfb_error(self, error: Exception) -> None:
        self.runtime_logger.log(
            "WARNING",
            __name__,
            "pfb.report",
            "pfb/a2 report failed",
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            error=error,
        )

    def _set_error(self, error: Exception) -> None:
        error_msg = str(error)
        # last_error 必须保存真实错误全文（截断防超长），
        # 否则前端只能看到"登录失败，请重试"而无法获知具体原因
        # （如：店铺身份已被其他店铺占用、登录态需验证等）。
        self.db.execute(
            "UPDATE shops SET status='error', last_error=%s WHERE id=%s",
            (error_msg[:500] if error_msg else "登录失败，请重试", self.shop_id),
        )
        if self.session_id:
            self.db.execute(
                "UPDATE shop_sessions SET status='error', ended_at=NOW(), error=%s WHERE id=%s",
                (error_msg, self.session_id),
            )
        self.runtime_logger.log(
            "ERROR",
            __name__,
            "shop.runtime",
            error_msg,
            shop_id=self.shop_id,
            mall_id=self.mall_id,
            error=error,
        )
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def _set_shop_status(self, status: str) -> None:
        self.db.execute("UPDATE shops SET status=%s WHERE id=%s", (status, self.shop_id))

    def _invalidate_login_cache(self) -> None:
        self.db.execute("DELETE FROM shop_login_caches WHERE shop_id=%s", (self.shop_id,))

    def _shop_row(self):
        return self.db.query_one(
            """
            SELECT s.*, EXISTS(SELECT 1 FROM shop_login_caches c WHERE c.shop_id=s.id) AS has_login_cache
            FROM shops s
            WHERE s.id=%s
            """,
            (self.shop_id,),
        )

    def _message_row(self, message_id: int):
        return self.db.query_one("SELECT * FROM messages WHERE id=%s", (message_id,))

    def _conversation_row(self, conversation_id: int):
        return self.db.query_one(
            """
            SELECT c.*, s.name AS shop_name, s.status AS shop_status,
                   s.auto_reply_enabled AS shop_auto_reply_enabled
            FROM conversations c
            JOIN shops s ON s.id=c.shop_id
            WHERE c.id=%s
            """,
            (conversation_id,),
        )
