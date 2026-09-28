"""Current-user profile and admin user-management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymysql.err import IntegrityError

from openkefu.web.context import (
    DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES,
    DEFAULT_SERVICE_MAX_SHOPS,
    AppContext,
    public_user,
)
from openkefu.web.deps import validate_user_password
from openkefu.web.security import hash_password


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    shop_ids: list[int] = []
    is_active: bool = True
    max_shops: int | None = None
    max_knowledge_bases: int | None = None
    max_llm_replies: int = 0


class UserUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    max_shops: int | None = None
    max_knowledge_bases: int | None = None
    max_llm_replies: int | None = None
    llm_reply_count: int | None = None


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/me")
    def me(user: dict[str, Any] = Depends(ctx.current_user)):
        return public_user(user)

    @router.get("/api/me/quota")
    def my_quota(user: dict[str, Any] = Depends(ctx.current_user)):
        row = ctx.db.query_one(
            "SELECT max_llm_replies, llm_reply_count, max_shops, max_knowledge_bases FROM users WHERE id=%s",
            (user["id"],),
        )
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        max_replies = int(row["max_llm_replies"] or 0)
        used = int(row["llm_reply_count"] or 0)
        return {
            "max_llm_replies": max_replies,
            "llm_reply_count": used,
            "remaining_llm_replies": max(0, max_replies - used),
            "max_shops": row["max_shops"],
            "max_knowledge_bases": row["max_knowledge_bases"],
        }

    @router.get("/api/users")
    def list_users(_: dict[str, Any] = Depends(ctx.require_admin)):
        users = ctx.db.query(
            """
            SELECT id, username, display_name, role, is_active, max_shops, max_knowledge_bases,
                   max_llm_replies, llm_reply_count, created_at
            FROM users
            ORDER BY id DESC
            """
        )
        assignments = ctx.db.query("SELECT shop_id, user_id FROM shop_assignments")
        by_user: dict[int, list[int]] = {}
        for item in assignments:
            by_user.setdefault(int(item["user_id"]), []).append(int(item["shop_id"]))
        return [{**public_user(item), "shop_ids": by_user.get(int(item["id"]), [])} for item in users]

    @router.post("/api/users")
    def create_user(body: UserCreate, _: dict[str, Any] = Depends(ctx.require_admin)):
        username = body.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        if ctx.db.query_one("SELECT id FROM users WHERE username=%s", (username,)):
            raise HTTPException(status_code=409, detail="username already exists")
        validate_user_password(body.password)
        if body.role not in {"admin", "service"}:
            raise HTTPException(status_code=400, detail="role must be admin or service")
        ctx.validate_non_negative_limit("max_llm_replies", body.max_llm_replies)
        ctx.validate_non_negative_limit("max_shops", body.max_shops)
        ctx.validate_non_negative_limit("max_knowledge_bases", body.max_knowledge_bases)
        max_shops = ctx.default_user_limit(body.role, body.max_shops, DEFAULT_SERVICE_MAX_SHOPS)
        max_knowledge_bases = ctx.default_user_limit(
            body.role,
            body.max_knowledge_bases,
            DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES,
        )
        try:
            user_id = ctx.db.execute(
                """
                INSERT INTO users
                (username, password_hash, display_name, role, is_active,
                 max_shops, max_knowledge_bases, max_llm_replies)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    username,
                    hash_password(body.password),
                    body.display_name,
                    body.role,
                    int(body.is_active),
                    max_shops,
                    max_knowledge_bases,
                    body.max_llm_replies,
                ),
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="username already exists") from exc
        for shop_id in body.shop_ids:
            ctx.db.execute(
                "INSERT IGNORE INTO shop_assignments (shop_id, user_id) VALUES (%s,%s)",
                (shop_id, user_id),
            )
        return ctx.db.query_one(
            """
            SELECT id, username, display_name, role, is_active, max_shops, max_knowledge_bases,
                   max_llm_replies, llm_reply_count
            FROM users
            WHERE id=%s
            """,
            (user_id,),
        )

    @router.patch("/api/users/{user_id}")
    def update_user(user_id: int, body: UserUpdate, _: dict[str, Any] = Depends(ctx.require_admin)):
        target = ctx.db.query_one("SELECT id, role FROM users WHERE id=%s", (user_id,))
        if not target:
            raise HTTPException(status_code=404, detail="user not found")
        fields = []
        values: list[Any] = []
        for name in ("display_name", "is_active", "max_shops", "max_knowledge_bases", "max_llm_replies", "llm_reply_count"):
            value = getattr(body, name)
            if value is None:
                continue
            if name in {"max_shops", "max_knowledge_bases"} and target["role"] == "admin":
                raise HTTPException(status_code=400, detail=f"{name} can only be modified for non-admin users")
            if name in {"max_shops", "max_knowledge_bases", "max_llm_replies", "llm_reply_count"} and value < 0:
                raise HTTPException(status_code=400, detail=f"{name} must be greater than or equal to 0")
            if name == "is_active":
                values.append(int(value))
            else:
                values.append(value)
            fields.append(f"{name}=%s")
        if body.password and body.password.strip():
            validate_user_password(body.password.strip())
            fields.append("password_hash=%s")
            values.append(hash_password(body.password.strip()))
            fields.append("auth_version=auth_version+1")
        if fields:
            values.append(user_id)
            ctx.db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=%s", values)
            ctx.hub.disconnect_user(user_id)
            if ctx.is_user_quota_exhausted(user_id):
                ctx.offline_user_active_shops(user_id)
        user = ctx.db.query_one(
            """
            SELECT id, username, display_name, role, is_active, max_shops, max_knowledge_bases,
                   max_llm_replies, llm_reply_count
            FROM users
            WHERE id=%s
            """,
            (user_id,),
        )
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        return public_user(user)

    return router
