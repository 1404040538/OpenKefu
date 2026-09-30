# -*- coding: utf-8 -*-
"""千牛店铺运行器（对标 PDD 的 ShopRunner，复用 openkefu 通用层）。

生命周期（与 PDD 端 web 层约定一致）：
  login_shop() 后台线程：出码(qr_pending) → 扫码(qr_scanned) → 登录成功(login_success)
  → cookie 加密入 shop_login_caches → 连接 impaas 长连接(online)
  on_message → hub 实时推送 + reply_fn 自动回复（可注入 LLM 链路）
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import requests

from openkefu.platforms.qianniu.auth.login import Login
from openkefu.platforms.qianniu.chat.impaas_client import ImpaasClient, ImpaasClientConfig
from openkefu.platforms.qianniu.chat.models import ImpaasMessage
from openkefu.platforms.qianniu.config import DEFAULT_USER_AGENT
from openkefu.web.config import AppConfig
from openkefu.web.crypto import TextCipher
from openkefu.web.db import Database, json_dumps
from openkefu.web.realtime import RealtimeHub, RuntimeLogger

logger = logging.getLogger(__name__)


class QianniuShopRunner:
    """千牛（淘宝商家）店铺运行态：扫码登录 + impaas 消息收发。"""

    def __init__(self, shop_id: int, db: Database, hub: RealtimeHub,
                 runtime_logger: RuntimeLogger, config: AppConfig, manager=None,
                 reply_fn: Callable[[ImpaasMessage, "QianniuShopRunner"], str | None] | None = None):
        from openkefu.web.repositories import Repositories
        self.shop_id = int(shop_id)
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.config = config
        self.manager = manager
        self.reply_fn = reply_fn
        self.repos = Repositories(db)
        self._secret_cipher = TextCipher(config.security.data_encryption_key)
        self.lock = threading.RLock()
        self.login: Login | None = None
        self.im_client: ImpaasClient | None = None
        self.session_id: int | None = None
        self.mall_id: str | None = None  # unb
        self.nick: str | None = None
        self.thread: threading.Thread | None = None
        # manager 兼容属性（千牛无账密登录）
        self._password_login_state: dict[str, Any] | None = None
        self.token_result: dict[str, Any] | None = None
        self.listener = None

    # ---------- 对外生命周期（对齐 ShopRunner 接口子集） ----------

    def start(self) -> dict[str, Any]:
        return self.login_shop()

    def login_shop(self) -> dict[str, Any]:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {"status": "already_running"}
            self.thread = threading.Thread(target=self._run_login_flow,
                                           name=f"qianniu-login-{self.shop_id}", daemon=True)
            self.thread.start()
        return {"status": "started"}

    def password_login_shop(self, username: str, password: str, verify_code: str = "") -> dict[str, Any]:
        raise RuntimeError("千牛平台暂仅支持扫码登录")

    def online(self) -> dict[str, Any]:
        row = self._shop_row()
        if self.im_client and self.im_client.status == "online":
            return {"status": "online", "shop": row}
        self._connect_from_cache()
        return {"status": self.im_client.status if self.im_client else "offline", "shop": row}

    def offline(self) -> None:
        self._disconnect(update_session=True, session_status="offline")

    def stop(self) -> None:
        self._disconnect(update_session=True, session_status="stopped")

    def latest_qrcode(self) -> str | None:
        attempt_id = self.repos.shops.latest_qr_attempt_id(self.shop_id)
        if not attempt_id:
            return None
        return f"/api/shops/{self.shop_id}/qrcode?attempt_id={attempt_id}"

    # ---------- 登录流程 ----------

    def _run_login_flow(self) -> None:
        try:
            self.login = Login()
            self.login.init_login_cookies()
            qrcode_info = self.login.get_qrcode()
            if not qrcode_info or not qrcode_info.get("lgToken"):
                raise RuntimeError("千牛登录二维码生成失败")

            self.session_id = self.repos.shops.create_session(self.shop_id, "qr_pending")
            attempt_id = self.repos.shops.create_qr_attempt(
                shop_id=self.shop_id, session_id=self.session_id,
                token=qrcode_info["lgToken"],
                qrcode_path=str(qrcode_info.get("qrImageFile") or ""),
            )
            self._set_shop_status("qr_pending")
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.hub.publish({"type": "qrcode", "data": {
                "shop_id": self.shop_id,
                "attempt_id": attempt_id,
                "qrcode_url": f"/api/shops/{self.shop_id}/qrcode?attempt_id={attempt_id}",
            }})
            self.runtime_logger.log("INFO", __name__, "shop.qrcode",
                                    "qianniu login qrcode generated", shop_id=self.shop_id)

            last_status = "qr_pending"

            def on_status(code: str, data: dict) -> None:
                nonlocal last_status
                status = None
                if code == "10001":
                    status = "qr_scanned"
                elif code == "10006":
                    status = "login_success"
                if not status or status == last_status:
                    return
                last_status = status
                self._set_login_progress(status, attempt_id, {"code": code})

            result = self.login.wait_for_login(qrcode_info["lgToken"],
                                               poll_interval=2, timeout=180,
                                               on_status=on_status)
            login_url = result.get("url") or ""
            if login_url and not login_url.startswith("http"):
                login_url = "https:" + login_url
            self.login.complete_login(login_url)

            self.mall_id = self.login.user_id or ""
            self.nick = self.login.nick or ""
            login_result = {
                "platform": "qianniu",
                "nick": self.nick,
                "user_id": self.mall_id,
                "cookies": self.login.get_target_cookies(),
                "all_cookies": {c.name: c.value for c in self.login.session.cookies},
            }
            with self.db.connect() as conn:
                with conn.cursor() as cursor:
                    self.repos.shops.succeed_qr_attempt(
                        attempt_id, json_dumps({"code": "10006", "nick": self.nick}), cursor=cursor)
                    self._store_login_cache_cursor(cursor, login_result)
                    self.repos.shops.mark_logged_in(self.shop_id, self.mall_id, cursor=cursor)
                    self.repos.shops.mark_session_logged_in(self.session_id, self.mall_id, cursor=cursor)
            self.hub.publish({"type": "shop_status", "data": self._shop_row()})
            self.runtime_logger.log("INFO", __name__, "shop.login",
                                    "qianniu login cache saved",
                                    shop_id=self.shop_id, mall_id=self.mall_id)
            self._connect_from_cache()
        except Exception as exc:
            logger.exception("qianniu login flow failed")
            self._set_error(exc)

    # ---------- 连接与消息 ----------

    def _connect_from_cache(self) -> None:
        cache = self.repos.shops.login_result_cache(self.shop_id) or {}
        login_result = self._load_secret_json(cache.get("login_result_json"), None)
        cookies = (login_result or {}).get("all_cookies") or (login_result or {}).get("cookies") or {}
        if not cookies:
            self._set_error(RuntimeError("千牛登录缓存为空，请重新扫码登录"))
            return
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": DEFAULT_USER_AGENT,
                                "Accept-Language": "zh-CN,zh;q=0.9"})
        for name, value in cookies.items():
            session.cookies.set(name, value, domain=".taobao.com", path="/")

        with self.lock:
            if self.im_client:
                self.im_client.stop()
            self.im_client = ImpaasClient(
                session, ImpaasClientConfig(),
                on_message=self._on_messages,
                on_status=self._on_im_status,
            )
            self.im_client.start()
        self.repos.shops.mark_online(self.shop_id, mall_id=self.mall_id or "",
                                     nickname=self.nick)
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def _on_im_status(self, status: str, detail: Any) -> None:
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})
        if status == "error":
            self.runtime_logger.log("WARNING", __name__, "qianniu.im", f"im status={status} {detail}",
                                    shop_id=self.shop_id)

    def _on_messages(self, messages: list[ImpaasMessage]) -> None:
        for message in messages:
            self.hub.publish({"type": "qianniu_message", "data": {
                "shop_id": self.shop_id,
                "cid": message.cid,
                "sender_uid": message.sender_uid_num,
                "text": message.text,
                "content_type": message.content_type,
                "message_id": message.message_id,
                "create_at": message.create_at,
            }})
        self.runtime_logger.log("INFO", __name__, "qianniu.message", "messages received",
                                shop_id=self.shop_id, count=len(messages))
        if self.reply_fn and self.im_client:
            for message in messages:
                try:
                    reply = self.reply_fn(message, self)
                except Exception:
                    logger.exception("qianniu reply_fn failed")
                    continue
                if not reply:
                    continue
                try:
                    peer = self._peer_uid(message.cid)
                    if peer:
                        self.im_client.send_text(message.cid, peer, reply)
                        self.runtime_logger.log("INFO", __name__, "qianniu.reply",
                                                 "auto reply sent", shop_id=self.shop_id,
                                                 cid=message.cid, length=len(reply))
                except Exception:
                    logger.exception("qianniu send reply failed")

    def _peer_uid(self, cid: str) -> str | None:
        head = cid.split("#", 1)[0]
        pair = head.split("-", 1)
        if len(pair) != 2:
            return None
        a = pair[0].rsplit(".", 1)[0]
        b = pair[1].rsplit(".", 1)[0]
        me = self.mall_id or (self.im_client.my_uid.split("@")[0] if self.im_client and self.im_client.my_uid else "")
        if a == me:
            return b
        if b == me:
            return a
        return b

    def _disconnect(self, *, update_session: bool, session_status: str = "offline") -> None:
        with self.lock:
            client, self.im_client = self.im_client, None
        if client:
            client.stop()
        if update_session and self.session_id:
            self.repos.shops.end_session(self.session_id, session_status)
            self.session_id = None
        self.repos.shops.set_status(self.shop_id, "idle")
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    # ---------- 通用辅助（约定同 ShopRunner） ----------

    def _set_login_progress(self, status: str, attempt_id: int, query_result: dict | None = None) -> None:
        self.repos.shops.update_qr_attempt(attempt_id, {
            "status": status, "query_result": json_dumps(query_result or {})})
        if self.session_id:
            self.repos.shops.update_session(self.session_id, {"status": status})
        self._set_shop_status(status)
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})

    def _store_login_cache_cursor(self, cursor: Any, login_result: dict) -> None:
        self.repos.shops.upsert_login_cache(
            shop_id=self.shop_id,
            cookies_json=self._encrypt_secret_text(json_dumps(login_result.get("cookies") or {})),
            cookie_string=self._encrypt_secret_text(""),
            requests_headers_json=self._encrypt_secret_text(json_dumps({})),
            base_headers_json=self._encrypt_secret_text(json_dumps({})),
            login_result_json=self._encrypt_secret_text(json_dumps(login_result)),
            cursor=cursor,
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

    def _set_error(self, error: Exception) -> None:
        error_msg = str(error)
        self.repos.shops.set_status_with_error(
            self.shop_id, "error", error_msg[:500] if error_msg else "登录失败，请重试")
        if self.session_id:
            self.repos.shops.end_session(self.session_id, "failed")
            self.session_id = None
        self.hub.publish({"type": "shop_status", "data": self._shop_row()})
        self.runtime_logger.log("ERROR", __name__, "shop.error", error_msg,
                                shop_id=self.shop_id, error=error)

    def _set_shop_status(self, status: str) -> None:
        self.repos.shops.set_status(self.shop_id, status)

    def _shop_row(self):
        return self.repos.shops.row_with_cache(self.shop_id)
