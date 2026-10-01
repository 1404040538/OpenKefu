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
import time
from typing import Any, Callable

import requests
from pymysql.err import IntegrityError

from openkefu.platforms.qianniu.auth.login import Login
from openkefu.platforms.qianniu.chat.impaas_client import ImpaasClient, ImpaasClientConfig
from openkefu.platforms.qianniu.chat.models import ImpaasMessage
from openkefu.platforms.qianniu.config import DEFAULT_USER_AGENT
from openkefu.services.conversation import build_conversation_messages
from openkefu.services.fast_reply import detect_fast_reply
from openkefu.services.intent import clean_reply_text
from openkefu.services.knowledge import KnowledgeService, NoteSetService
from openkefu.services.llm import LLMClient, get_llm_client
from openkefu.services.llm_reply import analyze_customer_intent
from openkefu.services.reply_cache import ReplyCache
from openkefu.web.config import AppConfig
from openkefu.web.crypto import TextCipher
from openkefu.web.db import Database, json_dumps
from openkefu.web.realtime import RealtimeHub, RuntimeLogger

logger = logging.getLogger(__name__)


class QianniuShopRunner:
    """千牛（淘宝商家）店铺运行态：扫码登录 + impaas 消息收发。"""

    def __init__(self, shop_id: int, db: Database, hub: RealtimeHub,
                 runtime_logger: RuntimeLogger, config: AppConfig, manager=None,
                 llm_client: LLMClient | None = None,
                 reply_cache: ReplyCache | None = None,
                 note_service: NoteSetService | None = None,
                 reply_fn: Callable[[ImpaasMessage, "QianniuShopRunner"], str | None] | None = None):
        from openkefu.web.repositories import Repositories
        self.shop_id = int(shop_id)
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.config = config
        self.manager = manager
        self.reply_fn = reply_fn  # 外部自定义回复钩子（优先于内置 LLM 管线）
        self.repos = Repositories(db)
        self._secret_cipher = TextCipher(config.security.data_encryption_key)
        self.llm_client = llm_client or get_llm_client(config.llm)
        self.knowledge = KnowledgeService(db, config)
        self.note_service = note_service or NoteSetService(db)
        self.reply_cache = reply_cache
        self.replied_keys: set[str] = set()
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

    def _complete_password_login(self, verify_code: str) -> dict[str, Any]:
        raise RuntimeError("千牛平台暂仅支持扫码登录")

    # ---------- manager 兼容方法（worker 启动恢复 / 每日重连任务调用） ----------

    def _connect_customer_service_from_cache(self) -> None:
        """对齐 PDD ShopRunner 约定：从登录缓存恢复客服连接。"""
        self._connect_from_cache()

    def _disconnect_customer_service(self, *, update_session: bool, session_status: str = "offline",
                                     cancel_reconnect: bool = True) -> None:
        self._disconnect(update_session=update_session, session_status=session_status)

    def hot_reconnect_customer_service(self, *, reason: str = "scheduled") -> bool:
        """每日定时热重连：重建 impaas 连接（重铸 token）。"""
        try:
            with self.lock:
                if self.im_client:
                    self.im_client.stop()
                    self.im_client = None
            self._connect_from_cache()
            return True
        except Exception as exc:
            logger.exception("qianniu hot reconnect failed: %s", reason)
            self.runtime_logger.log("WARNING", __name__, "qianniu.reconnect",
                                    f"hot reconnect failed: {exc}", shop_id=self.shop_id)
            return False

    def send_reply(self, *, conversation_id: int, content: str,
                   source_message_id: int | None = None, auto: bool = False,
                   send_attempts: int = 2) -> dict[str, Any]:
        """web 端手动回复（runtime_call 兼容签名）。"""
        conversation = self.repos.conversations.by_id_plain(conversation_id)
        if not conversation or int(conversation.get("shop_id") or 0) != self.shop_id:
            raise RuntimeError("会话不存在或不属于当前店铺")
        if not self.im_client or self.im_client.status != "online":
            raise RuntimeError("shop is not online")
        cid = str(conversation.get("conv_id") or "")
        peer = self._peer_uid(cid)
        if not cid or not peer:
            raise RuntimeError("会话缺少千牛 cid 信息，无法发送")
        content = clean_reply_text(content)
        if not content:
            raise RuntimeError("回复内容为空")
        last_error: Exception | None = None
        for _ in range(max(1, send_attempts)):
            try:
                result = self.im_client.send_text(cid, peer, content)
                self.repos.conversations.insert_chat_message({
                    "shop_id": self.shop_id,
                    "conversation_id": conversation_id,
                    "direction": "outbound",
                    "msg_id": str(result.get("messageId") or ""),
                    "client_msg_id": "",
                    "user_uid": str(conversation.get("user_uid") or ""),
                    "sender_role": "service",
                    "message_type": 1,
                    "kind": "text",
                    "content": content,
                    "goods_json": None,
                    "size_json": None,
                    "raw_json": None,
                    "message_at": int(time.time() * 1000),
                })
                self.hub.publish({"type": "message_sent", "data": {
                    "shop_id": self.shop_id, "conversation_id": conversation_id,
                    "content": content[:200],
                }})
                return {"success": True, "message_id": result.get("messageId")}
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"发送失败: {last_error}")

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
            # 入库（幂等：msg_id 唯一）
            conversation_id = self._store_message(message)
            self.hub.publish({"type": "qianniu_message", "data": {
                "shop_id": self.shop_id,
                "conversation_id": conversation_id,
                "cid": message.cid,
                "sender_uid": message.sender_uid_num,
                "text": message.text,
                "content_type": message.content_type,
                "message_id": message.message_id,
                "create_at": message.create_at,
            }})
        self.runtime_logger.log("INFO", __name__, "qianniu.message", "messages received",
                                shop_id=self.shop_id, count=len(messages))
        # 卡片消息（contentType 101）是浏览上下文，不触发自动回复
        text_messages = [m for m in messages if m.content_type == 1]
        if not (text_messages and self._auto_reply_enabled() and self.im_client):
            return
        for message in text_messages:
            conversation = self.repos.conversations.by_shop_uid_mall(
                self.shop_id, self._peer_uid(message.cid) or "", self.mall_id or None)
            conversation_id = int(conversation["id"]) if conversation else 0
            # 外部钩子优先
            reply = None
            if self.reply_fn:
                try:
                    reply = self.reply_fn(message, self)
                except Exception:
                    logger.exception("qianniu reply_fn failed")
            if reply:
                self._deliver_reply(message, conversation_id, reply, source="hook")
                continue
            try:
                self._auto_reply(message, conversation_id)
            except Exception:
                logger.exception("qianniu auto reply failed")

    def _deliver_reply(self, message: ImpaasMessage, conversation_id: int,
                       content: str, *, source: str) -> None:
        peer = self._peer_uid(message.cid)
        if not peer or not self.im_client:
            return
        try:
            result = self.im_client.send_text(message.cid, peer, content)
            self._store_outbound_message(message, str(result.get("messageId") or ""), content)
            self.runtime_logger.log("INFO", __name__, "qianniu.reply",
                                     "reply sent", shop_id=self.shop_id,
                                     cid=message.cid, source=source, length=len(content))
        except Exception:
            logger.exception("qianniu send reply failed")

    # ---------- 自动回复管线（知识库 → 快捷回复 → 缓存 → LLM） ----------

    def _auto_reply(self, message: ImpaasMessage, conversation_id: int) -> None:
        content = (message.text or "").strip()
        if not content:
            return
        dedupe_key = f"{message.message_id}"
        with self.lock:
            if dedupe_key in self.replied_keys:
                return
            self.replied_keys.add(dedupe_key)
            if len(self.replied_keys) > 20000:
                for _ in range(6666):
                    self.replied_keys.pop()

        # 1. 知识库问答直配
        qa_match = self.knowledge.find_qa_match_for_shop(shop_id=self.shop_id, query=content)
        if qa_match:
            self._deliver_reply(message, conversation_id, str(qa_match.get("answer") or ""), source="knowledge")
            return

        # 2. 快捷回复
        fast = detect_fast_reply(content)
        if fast:
            self._deliver_reply(message, conversation_id, fast, source="fast")
            return

        # 3. 回复缓存
        if self.reply_cache:
            cached = self.reply_cache.get(content, self.shop_id)
            if cached and cached.get("reply"):
                self._deliver_reply(message, conversation_id, str(cached["reply"]), source="cache")
                return

        # 4. LLM 意图分析生成
        history = self._llm_history(conversation_id)
        compressed = build_conversation_messages(history, self.llm_client)
        knowledge_hits = self.knowledge.search_for_shop(
            shop_id=self.shop_id, query=self._knowledge_query(history), top_k=5)
        shop_notes = self.note_service.get_items_for_shop(self.shop_id)
        intent = analyze_customer_intent(
            compressed,
            knowledge_hits=knowledge_hits,
            shop_notes=shop_notes or None,
            llm_client=self.llm_client,
        )
        reply = clean_reply_text(str(intent.get("reply") or ""))
        if not reply:
            self.runtime_logger.log("INFO", __name__, "qianniu.reply.empty",
                                     "llm produced empty reply", shop_id=self.shop_id,
                                     cid=message.cid)
            return
        self._deliver_reply(message, conversation_id, reply, source="llm")
        # 记录意图事件（对齐 PDD 的分析留痕）
        try:
            self.repos.conversations.store_intent_event({
                "shop_id": self.shop_id,
                "conversation_id": conversation_id or None,
                "user_uid": message.sender_uid_num,
                "intent_code": str(intent.get("intent_code") or ""),
                "raw_intent_code": str(intent.get("raw_intent_code") or ""),
                "confidence": intent.get("confidence"),
                "reply_text": reply[:2000],
                "raw_json": json_dumps({"source": "qianniu", "cid": message.cid}),
            })
        except Exception:
            logger.exception("store_intent_event failed")

    def _llm_history(self, conversation_id: int) -> list[dict[str, str]]:
        if not conversation_id:
            return []
        rows = self.db.query(
            """
            SELECT direction, content FROM messages
            WHERE conversation_id=%s AND direction IN ('inbound','outbound')
            ORDER BY id DESC LIMIT 10
            """,
            (conversation_id,),
        )
        history = []
        for row in reversed(rows):
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            role = "assistant" if row.get("direction") == "outbound" else "user"
            history.append({"role": role, "content": content})
        return history

    @staticmethod
    def _knowledge_query(history: list[dict[str, str]]) -> str:
        for item in reversed(history):
            if item.get("role") == "user" and item.get("content"):
                return str(item["content"])
        return history[-1]["content"] if history else ""

    def _auto_reply_enabled(self) -> bool:
        state = self.repos.shops.auto_reply_state(self.shop_id)
        return bool(state and state.get("auto_reply_enabled"))

    # ---------- 消息持久化（沿用 conversations/messages 表约定） ----------

    def _store_message(self, message: ImpaasMessage) -> int:
        """买家消息入库（幂等），返回 conversation_id。"""
        try:
            existing = self.repos.conversations.existing_message_row(
                self.shop_id, msg_id=message.message_id or None)
            if existing:
                return int(existing.get("conversation_id") or 0)
        except Exception:
            logger.exception("qianniu existing_message_row failed")

        user_uid = self._peer_uid(message.cid) or message.sender_uid_num
        is_card = message.content_type != 1
        mall_id = self.mall_id or None
        params = {
            "shop_id": self.shop_id,
            "mall_id": mall_id,
            "conv_id": message.cid,
            "chat_type_id": "",
            "chat_type": "qianniu_kefu",
            "user_uid": user_uid,
            "nickname": "",
            "preview": (message.text or "")[:512],
            "message_at": message.create_at or int(time.time() * 1000),
            "unread_delta": 0 if is_card else 1,
        }
        try:
            conversation = self.repos.conversations.by_shop_uid_mall(self.shop_id, user_uid, mall_id)
            if conversation:
                conversation_id = int(conversation["id"])
                self.repos.conversations.touch_on_message(conversation_id, params)
            else:
                conversation_id = self.repos.conversations.create_from_incoming(params)
        except IntegrityError:
            conversation = self.repos.conversations.by_shop_uid_mall(self.shop_id, user_uid, mall_id)
            if not conversation:
                raise
            conversation_id = int(conversation["id"])

        try:
            self.repos.conversations.insert_chat_message({
                "shop_id": self.shop_id,
                "conversation_id": conversation_id,
                "direction": "system" if is_card else "inbound",
                "msg_id": message.message_id,
                "client_msg_id": "",
                "user_uid": user_uid,
                "sender_role": "system" if is_card else "user",
                "message_type": message.content_type,
                "kind": "card" if is_card else "text",
                "content": message.text or "",
                "goods_json": None,
                "size_json": None,
                "raw_json": json_dumps({"cid": message.cid, "sender": message.sender_uid,
                                        "contentType": message.content_type,
                                        "createAt": message.create_at}),
                "message_at": message.create_at or int(time.time() * 1000),
            })
        except IntegrityError:
            pass  # 并发重复推送，幂等
        return conversation_id

    def _store_outbound_message(self, source: ImpaasMessage, msg_id: str, content: str) -> None:
        """自动回复入库（direction=outbound，对齐人工回复约定）。"""
        try:
            conversation = self.repos.conversations.by_shop_uid_mall(
                self.shop_id, self._peer_uid(source.cid) or "", self.mall_id or None)
            if not conversation:
                return
            self.repos.conversations.insert_chat_message({
                "shop_id": self.shop_id,
                "conversation_id": int(conversation["id"]),
                "direction": "outbound",
                "msg_id": msg_id,
                "client_msg_id": "",
                "user_uid": source.sender_uid_num,
                "sender_role": "service",
                "message_type": 1,
                "kind": "text",
                "content": content,
                "goods_json": None,
                "size_json": None,
                "raw_json": None,
                "message_at": int(time.time() * 1000),
            })
        except Exception:
            logger.exception("qianniu store outbound failed")

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
