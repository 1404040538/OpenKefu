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
from openkefu.web.repositories import Repositories
from openkefu.web.security import create_token, decode_token

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
    }



def build_return_record_where(
    *,
    is_admin: bool,
    shop_id: int | None,
    allowed_shop_ids: set[int],
    record_type: str | None,
    keyword: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[Any]]:
    where = ["1=1"]
    values: list[Any] = []
    if shop_id is not None:
        where.append("rr.shop_id=%s")
        values.append(shop_id)
    elif not is_admin:
        if not allowed_shop_ids:
            where.append("0=1")
        else:
            where.append("rr.shop_id IN (" + ",".join(["%s"] * len(allowed_shop_ids)) + ")")
            values.extend(sorted(allowed_shop_ids))
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


class AppContext:
    """Shared state and helpers for all API routers."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        hub: RealtimeHub,
        runtime_logger: RuntimeLogger,
        runtime,
        knowledge: KnowledgeService,
        note_service: NoteSetService,
    ):
        self.config = config
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.runtime = runtime
        self.knowledge = knowledge
        self.note_service = note_service
        self.repos = Repositories(db)

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
        user = self.repos.users.active_auth_user(int(payload.get("sub") or 0))
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
        return self.repos.users.get_setting(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.repos.users.set_setting(key, value)

    def registration_enabled(self) -> bool:
        return bool(self.get_setting("registration_enabled", True))

    # ------------------------------------------------------------------
    # access control
    # ------------------------------------------------------------------
    def can_access_shop(self, user: dict[str, Any], shop_id: int) -> None:
        if user["role"] == "admin":
            return
        if not self.repos.users.has_assignment(shop_id, int(user["id"])):
            raise HTTPException(status_code=403, detail="shop not assigned")

    def assigned_shop_ids(self, user: dict[str, Any]) -> set[int]:
        if user["role"] == "admin":
            return self.repos.shops.all_shop_ids()
        return self.repos.users.assigned_shop_ids(int(user["id"]))

    def ensure_shop_ids_manageable(self, user: dict[str, Any], shop_ids: list[int]) -> None:
        allowed = self.assigned_shop_ids(user)
        for shop_id in sorted({int(item) for item in shop_ids}):
            if shop_id not in allowed:
                if not self.repos.shops.exists(shop_id):
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
        shop = self.repos.conversations.shop_mall_row(int(conversation["shop_id"]))
        current_mall_id = str((shop or {}).get("mall_id") or "")
        conversation_mall_id = str(conversation.get("mall_id") or "")
        if not current_mall_id or not conversation_mall_id or current_mall_id != conversation_mall_id:
            raise HTTPException(status_code=404, detail="conversation not found")

    def conversation_row(self, conversation_id: int) -> dict[str, Any] | None:
        return self.repos.conversations.row(conversation_id)

    def publish_conversation(self, conversation_id: int) -> None:
        row = self.repos.conversations.row(conversation_id)
        if row:
            self.hub.publish({"type": "conversation", "data": row})

    def delete_shop_chat_records(self, shop_id: int) -> None:
        self.repos.shops.delete_shop_chat_records(shop_id)

    def normalize_shop_runtime_status(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active_shop_ids = self.repos.shops.active_leased_shop_ids(
            [int(row["id"]) for row in rows if row.get("id") is not None]
        )
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
        if shop_id is not None:
            self.can_access_shop(user, shop_id)
        allowed = self.assigned_shop_ids(user)
        return build_return_record_where(
            is_admin=user["role"] == "admin",
            shop_id=shop_id,
            allowed_shop_ids=allowed,
            record_type=record_type,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
        )

    def serialize_return_record(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result.pop("slots_json", None)
        result.pop("order_snapshot_json", None)
        return result
        # （查询/写入已收敛到 ReturnRecordsRepository）

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
            if not self.repos.shops.exists(normalized["shop_id"]):
                raise HTTPException(status_code=404, detail="shop not found")
        return normalized

    def return_record_by_id(self, record_id: int, user: dict[str, Any]) -> dict[str, Any]:
        row = self.repos.return_records.by_id(record_id)
        if not row:
            raise HTTPException(status_code=404, detail="return record not found")
        self.can_access_shop(user, int(row["shop_id"]))
        return row

    def upsert_return_record(self, data: dict[str, Any], *, existing_id: int | None = None) -> dict[str, Any]:
        return self.repos.return_records.upsert(data, existing_id=existing_id)
