"""Shared application context: state plus cross-domain helpers used by routers.

Each router module receives an :class:`AppContext` instance when built via its
``build_router(ctx)`` factory, which keeps the previous single-closure layout of
``create_app`` but in decoupled modules.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Depends, Header, HTTPException

from openkefu.platforms.pdd.chat.knowledge import KnowledgeService, NoteSetService
from openkefu.web.config import AppConfig
from openkefu.web.crypto import TextCipher
from openkefu.web.db import Database, json_dumps
from openkefu.web.ratelimit import FixedWindowRateLimiter
from openkefu.web.realtime import RealtimeHub, RuntimeLogger
from openkefu.web.security import create_token, decode_token

DEFAULT_REGISTER_LLM_REPLIES = 100
DEFAULT_SERVICE_MAX_SHOPS = 10
DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES = 5
DEFAULT_SERVICE_MAX_KNOWLEDGE_FILES = 3
DEFAULT_MAX_FILE_BYTES_GLOBAL = 20 * 1024 * 1024

LOGIN_RATE_LIMIT_HITS = 5
LOGIN_RATE_LIMIT_WINDOW = 900
REGISTER_RATE_LIMIT_HITS = 3
REGISTER_RATE_LIMIT_WINDOW = 3600
SETUP_ADMIN_RATE_LIMIT_HITS = 5
SETUP_ADMIN_RATE_LIMIT_WINDOW = 3600


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "is_active": bool(user["is_active"]),
        "max_shops": user.get("max_shops"),
        "max_knowledge_bases": user.get("max_knowledge_bases"),
        "max_llm_replies": user.get("max_llm_replies", 0),
        "llm_reply_count": user.get("llm_reply_count", 0),
    }


class AppContext:
    """Shared state and helpers for all API routers."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        hub: RealtimeHub,
        runtime_logger: RuntimeLogger,
        runtime,
        server_status,
        knowledge: KnowledgeService,
        note_service: NoteSetService,
    ):
        self.config = config
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.runtime = runtime
        self.server_status = server_status
        self.knowledge = knowledge
        self.note_service = note_service

        limiter_enabled = not config.security.rate_limit_disabled
        self.login_limiter = FixedWindowRateLimiter(LOGIN_RATE_LIMIT_HITS, LOGIN_RATE_LIMIT_WINDOW, enabled=limiter_enabled)
        self.register_limiter = FixedWindowRateLimiter(REGISTER_RATE_LIMIT_HITS, REGISTER_RATE_LIMIT_WINDOW, enabled=limiter_enabled)
        self.setup_admin_limiter = FixedWindowRateLimiter(SETUP_ADMIN_RATE_LIMIT_HITS, SETUP_ADMIN_RATE_LIMIT_WINDOW, enabled=limiter_enabled)
        self.offline_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="offline-shop")
        self.require_admin = self._build_require_admin()

    # ------------------------------------------------------------------
    # runtime / auth
    # ------------------------------------------------------------------
    def runtime_call(self, action: str, shop_id: int | None = None, params: dict[str, Any] | None = None, *, timeout: float = 30.0):
        if self.config.runtime.role == "api":
            return self.runtime.command_bus.call(action, shop_id, params or {}, timeout=timeout)
        return self.runtime._execute_runtime_action(action, shop_id, params or {})

    def active_user_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        user = self.db.query_one(
            """
            SELECT id, username, display_name, role, is_active, auth_version,
                   max_shops, max_knowledge_bases, max_llm_replies, llm_reply_count
            FROM users
            WHERE id=%s
            """,
            (payload.get("sub"),),
        )
        if not user or not user.get("is_active"):
            raise HTTPException(status_code=401, detail="inactive user")
        if int(payload.get("auth_version") or 0) != int(user.get("auth_version") or 1):
            raise HTTPException(status_code=401, detail="token has been revoked")
        return user

    def current_user(self, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing token")
        try:
            payload = decode_token(authorization.removeprefix("Bearer ").strip(), secret=self.config.security.jwt_secret)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return self.active_user_from_payload(payload)

    def require_admin_user(self, user: dict[str, Any]) -> dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return user

    def _build_require_admin(self):
        def require_admin(user: dict[str, Any] = Depends(self.current_user)) -> dict[str, Any]:
            return self.require_admin_user(user)
        return require_admin

    def auth_response(self, user: dict[str, Any]) -> dict[str, Any]:
        token = create_token(
            {
                "sub": user["id"],
                "username": user["username"],
                "role": user["role"],
                "auth_version": int(user.get("auth_version") or 1),
            },
            secret=self.config.security.jwt_secret,
            expire_minutes=self.config.security.jwt_expire_minutes,
        )
        return {"token": token, "user": public_user(user)}

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value_json FROM app_settings WHERE `key`=%s", (key,))
        if not row:
            return default
        try:
            return json.loads(str(row.get("value_json") or "null"))
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO app_settings (`key`, value_json)
            VALUES (%s,%s)
            ON DUPLICATE KEY UPDATE value_json=VALUES(value_json)
            """,
            (key, json_dumps(value)),
        )

    def registration_enabled(self) -> bool:
        return bool(self.get_setting("registration_enabled", True))

    # ------------------------------------------------------------------
    # quotas
    # ------------------------------------------------------------------
    def default_user_limit(self, role: str, value: int | None, default: int) -> int | None:
        if role == "admin":
            return None
        return default if value is None else int(value)

    def validate_non_negative_limit(self, name: str, value: int | None) -> None:
        if value is not None and value < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be greater than or equal to 0")

    def current_limit(self, user: dict[str, Any], key: str, default: int) -> int | None:
        if user["role"] == "admin":
            return None
        value = user.get(key)
        return default if value is None else int(value)

    def count_user_shops(self, user_id: int) -> int:
        row = self.db.query_one(
            """
            SELECT COUNT(DISTINCT s.id) AS cnt
            FROM shops s
            LEFT JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE s.created_by_user_id=%s OR s.created_by=%s OR sa.user_id=%s
            """,
            (user_id, user_id, user_id),
        )
        return int((row or {}).get("cnt") or 0)

    def count_user_knowledge_bases(self, user_id: int) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS cnt FROM knowledge_bases WHERE created_by=%s", (user_id,))
        return int((row or {}).get("cnt") or 0)

    def ensure_user_can_create_shop(self, user: dict[str, Any]) -> None:
        limit = self.current_limit(user, "max_shops", DEFAULT_SERVICE_MAX_SHOPS)
        if limit is not None and self.count_user_shops(int(user["id"])) >= limit:
            raise HTTPException(status_code=403, detail="店铺数量已达上限")

    def ensure_user_can_create_knowledge_base(self, user: dict[str, Any]) -> None:
        limit = self.current_limit(user, "max_knowledge_bases", DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES)
        if limit is not None and self.count_user_knowledge_bases(int(user["id"])) >= limit:
            raise HTTPException(status_code=403, detail="知识库数量已达上限")

    def ensure_user_can_upload_knowledge_file(self, user: dict[str, Any], kb_id: int, data: bytes) -> None:
        if len(data) > DEFAULT_MAX_FILE_BYTES_GLOBAL:
            raise HTTPException(status_code=400, detail="文件大小不能超过 20MB")
        if user["role"] == "admin":
            return
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM knowledge_files WHERE knowledge_base_id=%s",
            (kb_id,),
        )
        if int((row or {}).get("cnt") or 0) >= DEFAULT_SERVICE_MAX_KNOWLEDGE_FILES:
            raise HTTPException(status_code=403, detail="单个知识库最多上传 3 个文件")

    def is_user_quota_exhausted(self, user_id: int) -> bool:
        row = self.db.query_one("SELECT role, max_llm_replies, llm_reply_count FROM users WHERE id=%s", (user_id,))
        if not row:
            return False
        # 管理员不受额度限制
        if str(row.get("role") or "") == "admin":
            return False
        max_replies = int(row.get("max_llm_replies") or 0)
        # 0 表示不限量（与 DB 注释 "0 means no quota" 一致）：只计数不拦截
        if max_replies <= 0:
            return False
        return int(row.get("llm_reply_count") or 0) >= max_replies

    def offline_user_active_shops(self, user_id: int) -> None:
        rows = self.db.query(
            """
            SELECT DISTINCT s.id
            FROM shops s
            LEFT JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE (s.created_by_user_id=%s OR sa.user_id=%s OR s.created_by=%s)
              AND s.status IN ('online','connecting','logged_in','login_pending','qr_pending')
            """,
            (user_id, user_id, user_id),
        )
        shop_ids = [int(row["id"]) for row in rows]

        def _offline_one(shop_id: int) -> None:
            try:
                self.runtime_call("offline_shop", shop_id, timeout=5)
            except Exception:
                self.db.execute("UPDATE shops SET status='offline' WHERE id=%s", (shop_id,))
                shop = self.db.query_one(
                    """
                    SELECT s.*, EXISTS(SELECT 1 FROM shop_login_caches c WHERE c.shop_id=s.id) AS has_login_cache
                    FROM shops s
                    WHERE s.id=%s
                    """,
                    (shop_id,),
                )
                if shop:
                    self.hub.publish({"type": "shop_status", "data": shop})

        if shop_ids:
            futures = [self.offline_pool.submit(_offline_one, shop_id) for shop_id in shop_ids]
            for future in futures:
                try:
                    future.result(timeout=10)
                except Exception:
                    pass
        self.hub.publish({"type": "llm_quota_exceeded", "data": {"user_id": user_id}})

    def shop_quota_user_id(self, shop_id: int, user: dict[str, Any]) -> int | None:
        shop = self.db.query_one("SELECT created_by_user_id, created_by FROM shops WHERE id=%s", (shop_id,))
        if not shop:
            return None
        if shop.get("created_by_user_id"):
            return int(shop["created_by_user_id"])
        assignments = self.db.query("SELECT user_id FROM shop_assignments WHERE shop_id=%s ORDER BY id LIMIT 2", (shop_id,))
        if len(assignments) == 1:
            return int(assignments[0]["user_id"])
        if shop.get("created_by"):
            return int(shop["created_by"])
        return None

    def ensure_shop_has_llm_quota(self, shop_id: int, user: dict[str, Any], error_message: str) -> None:
        quota_user_id = self.shop_quota_user_id(shop_id, user)
        if quota_user_id is None:
            raise HTTPException(status_code=403, detail="无法确认店铺额度归属，禁止操作店铺")
        if self.is_user_quota_exhausted(quota_user_id):
            self.offline_user_active_shops(quota_user_id)
            raise HTTPException(status_code=403, detail=error_message)

    def reserve_user_llm_quota(self, user_id: int) -> bool:
        user_row = self.db.query_one("SELECT role, max_llm_replies FROM users WHERE id=%s", (user_id,))
        if not user_row:
            raise HTTPException(status_code=403, detail="大模型调用额度不足")
        if str(user_row.get("role") or "") == "admin":
            # 管理员不受额度限制：只计数不拦截
            self.db.execute("UPDATE users SET llm_reply_count = llm_reply_count + 1 WHERE id=%s", (user_id,))
            return False
        max_replies = int(user_row.get("max_llm_replies") or 0)
        if max_replies <= 0:
            # 0 表示不限量：只计数不拦截
            self.db.execute("UPDATE users SET llm_reply_count = llm_reply_count + 1 WHERE id=%s", (user_id,))
            return False
        affected = self.db.execute(
            """
            UPDATE users
            SET llm_reply_count = llm_reply_count + 1
            WHERE id=%s AND llm_reply_count < max_llm_replies
            """,
            (user_id,),
        )
        if affected < 1:
            self.offline_user_active_shops(user_id)
            raise HTTPException(status_code=403, detail="大模型调用额度不足")
        return self.is_user_quota_exhausted(user_id)

    # ------------------------------------------------------------------
    # access control
    # ------------------------------------------------------------------
    def can_access_shop(self, user: dict[str, Any], shop_id: int) -> None:
        if user["role"] == "admin":
            return
        row = self.db.query_one(
            "SELECT id FROM shop_assignments WHERE shop_id=%s AND user_id=%s",
            (shop_id, user["id"]),
        )
        if not row:
            raise HTTPException(status_code=403, detail="shop not assigned")

    def assigned_shop_ids(self, user: dict[str, Any]) -> set[int]:
        if user["role"] == "admin":
            return {int(row["id"]) for row in self.db.query("SELECT id FROM shops")}
        return {
            int(row["shop_id"])
            for row in self.db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user["id"],))
        }

    def ensure_shop_ids_manageable(self, user: dict[str, Any], shop_ids: list[int]) -> None:
        allowed = self.assigned_shop_ids(user)
        for shop_id in sorted({int(item) for item in shop_ids}):
            if shop_id not in allowed:
                if not self.db.query_one("SELECT id FROM shops WHERE id=%s", (shop_id,)):
                    raise HTTPException(status_code=400, detail=f"shop not found: {shop_id}")
                raise HTTPException(status_code=403, detail=f"shop not assigned: {shop_id}")

    def can_manage_owned_row(self, user: dict[str, Any], row: dict[str, Any]) -> bool:
        if user["role"] == "admin":
            return True
        return int(row.get("created_by") or 0) == int(user["id"])

    def ensure_knowledge_base_manageable(self, kb_id: int, user: dict[str, Any]) -> dict[str, Any]:
        try:
            row = self.knowledge.get_knowledge_base(kb_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not self.can_manage_owned_row(user, row):
            raise HTTPException(status_code=403, detail="knowledge base belongs to another user")
        return row

    def ensure_note_set_manageable(self, ns_id: int, user: dict[str, Any]) -> dict[str, Any]:
        try:
            row = self.note_service.get_note_set(ns_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not self.can_manage_owned_row(user, row):
            raise HTTPException(status_code=403, detail="note set belongs to another user")
        return row

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    def ensure_current_conversation(self, conversation: dict[str, Any]) -> None:
        shop = self.db.query_one("SELECT mall_id FROM shops WHERE id=%s", (conversation["shop_id"],))
        current_mall_id = str((shop or {}).get("mall_id") or "")
        conversation_mall_id = str(conversation.get("mall_id") or "")
        if not current_mall_id or not conversation_mall_id or current_mall_id != conversation_mall_id:
            raise HTTPException(status_code=404, detail="conversation not found")

    def conversation_select_sql(self) -> str:
        return """
            SELECT c.*, s.name AS shop_name, s.status AS shop_status,
                   s.auto_reply_enabled AS shop_auto_reply_enabled
            FROM conversations c
            JOIN shops s ON s.id=c.shop_id
        """

    def conversation_order_sql(self) -> str:
        return """
            ORDER BY c.human_attention_required DESC,
                     c.human_attention_at DESC,
                     c.updated_at DESC
        """

    def conversation_row(self, conversation_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            f"""
            {self.conversation_select_sql()}
            WHERE c.id=%s
            """,
            (conversation_id,),
        )

    def publish_conversation(self, conversation_id: int) -> None:
        row = self.conversation_row(conversation_id)
        if row:
            self.hub.publish({"type": "conversation", "data": row})

    def delete_shop_chat_records(self, shop_id: int) -> None:
        with self.db.connect() as conn:
            with conn.cursor() as cursor:
                for table in (
                    "return_record_drafts",
                    "return_records",
                    "action_requests",
                    "intent_events",
                    "transfer_attempts",
                    "reply_attempts",
                    "messages",
                    "conversations",
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE shop_id=%s", (shop_id,))

    def normalize_shop_runtime_status(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shop_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if not shop_ids:
            return rows
        placeholders = ",".join(["%s"] * len(shop_ids))
        active_rows = self.db.query(
            f"""
            SELECT shop_id
            FROM shop_runtime_leases
            WHERE shop_id IN ({placeholders})
              AND expires_at>=NOW()
              AND status='online'
            """,
            shop_ids,
        )
        active_shop_ids = {int(row["shop_id"]) for row in active_rows}
        for row in rows:
            status = str(row.get("status") or "")
            shop_id = int(row.get("id") or 0)
            if status in {"online", "connecting"} and shop_id not in active_shop_ids:
                row["status"] = "offline"
        return rows

    # ------------------------------------------------------------------
    # return records
    # ------------------------------------------------------------------
    def return_record_where(
        self,
        user: dict[str, Any],
        *,
        shop_id: int | None,
        record_type: str | None,
        keyword: str | None,
        date_from: str | None,
        date_to: str | None,
    ) -> tuple[str, list[Any]]:
        where = ["1=1"]
        values: list[Any] = []
        allowed = self.assigned_shop_ids(user)
        if shop_id is not None:
            self.can_access_shop(user, shop_id)
            where.append("rr.shop_id=%s")
            values.append(shop_id)
        elif user["role"] != "admin":
            if not allowed:
                where.append("0=1")
            else:
                where.append("rr.shop_id IN (" + ",".join(["%s"] * len(allowed)) + ")")
                values.extend(sorted(allowed))
        if record_type:
            where.append("rr.record_type=%s")
            values.append(record_type)
        if keyword:
            like = f"%{keyword.strip()}%"
            where.append(
                "(rr.username LIKE %s OR rr.user_uid LIKE %s OR rr.order_no LIKE %s "
                "OR rr.order_status LIKE %s OR rr.new_address LIKE %s OR rr.remark LIKE %s "
                "OR rr.source_message LIKE %s)"
            )
            values.extend([like] * 7)
        if date_from:
            where.append("rr.created_at >= %s")
            values.append(date_from)
        if date_to:
            where.append("rr.created_at <= %s")
            values.append(date_to)
        return " WHERE " + " AND ".join(where), values

    def serialize_return_record(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result.pop("slots_json", None)
        result.pop("order_snapshot_json", None)
        return result

    def validate_return_record_payload(self, data: dict[str, Any], *, user: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        allowed_types = {"return", "exchange", "address_change", "refund_only", "logistics_intercept", "other"}
        normalized = dict(data)
        for key in ("user_uid", "username", "order_no", "order_status", "record_type", "new_address", "remark", "source_message"):
            if key in normalized and normalized[key] is not None:
                normalized[key] = str(normalized[key]).strip()
        required = ("shop_id", "user_uid", "order_no", "record_type")
        if not partial:
            missing = [key for key in required if not normalized.get(key)]
            if missing:
                raise HTTPException(status_code=400, detail=f"missing required fields: {', '.join(missing)}")
        if normalized.get("record_type") and normalized["record_type"] not in allowed_types:
            raise HTTPException(status_code=400, detail="invalid record_type")
        if normalized.get("shop_id") is not None:
            normalized["shop_id"] = int(normalized["shop_id"])
            self.can_access_shop(user, normalized["shop_id"])
            if not self.db.query_one("SELECT id FROM shops WHERE id=%s", (normalized["shop_id"],)):
                raise HTTPException(status_code=404, detail="shop not found")
        return normalized

    def return_record_by_id(self, record_id: int, user: dict[str, Any]) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT rr.*, s.name AS shop_name
            FROM return_records rr
            JOIN shops s ON s.id=rr.shop_id
            WHERE rr.id=%s
            """,
            (record_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="return record not found")
        self.can_access_shop(user, int(row["shop_id"]))
        return row

    def upsert_return_record(self, data: dict[str, Any], *, existing_id: int | None = None) -> dict[str, Any]:
        existing: dict[str, Any] | None = None
        if data.get("shop_id") and data.get("user_uid") and data.get("order_no"):
            rows = self.db.query(
                """
                SELECT id FROM return_records
                WHERE shop_id=%s AND user_uid=%s AND order_no=%s
                ORDER BY id DESC
                """,
                (data["shop_id"], data["user_uid"], data["order_no"]),
            )
            for row in rows:
                if existing_id is None or int(row["id"]) != int(existing_id):
                    existing = row
                    break
        target_id = int(existing["id"]) if existing else existing_id
        fields = [
            "shop_id", "conversation_id", "message_id", "user_uid", "username", "order_no",
            "order_status", "record_type", "new_address", "remark", "source_message",
        ]
        values = {key: data.get(key) for key in fields}
        values["username"] = values.get("username") or values.get("user_uid") or ""
        values["order_status"] = values.get("order_status") or "待核实"
        if target_id:
            assignments = ", ".join(f"{key}=%s" for key in fields) + ", slots_json=NULL, order_snapshot_json=NULL, created_at=NOW()"
            self.db.execute(
                f"UPDATE return_records SET {assignments} WHERE id=%s",
                [values.get(key) for key in fields] + [target_id],
            )
            if existing and existing_id and int(existing["id"]) != int(existing_id):
                self.db.execute("DELETE FROM return_records WHERE id=%s", (existing_id,))
            row = self.db.query_one(
                """
                SELECT rr.*, s.name AS shop_name
                FROM return_records rr
                JOIN shops s ON s.id=rr.shop_id
                WHERE rr.id=%s
                """,
                (target_id,),
            )
            return row or {}
        record_id = self.db.execute(
            """
            INSERT INTO return_records
            (shop_id, conversation_id, message_id, user_uid, username, order_no, order_status,
             record_type, new_address, remark, source_message, slots_json, order_snapshot_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL)
            """,
            [values.get(key) for key in fields],
        )
        row = self.db.query_one(
            """
            SELECT rr.*, s.name AS shop_name
            FROM return_records rr
            JOIN shops s ON s.id=rr.shop_id
            WHERE rr.id=%s
            """,
            (record_id,),
        )
        return row or {}
