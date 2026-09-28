"""Current-user profile and admin user-management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymysql.err import IntegrityError

from openkefu.web.context import AppContext, public_user
from openkefu.web.deps import validate_user_password
from openkefu.web.security import hash_password


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    shop_ids: list[int] = []
    is_active: bool = True


class UserUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    is_active: bool | None = None


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/me")
    def me(user: dict[str, Any] = Depends(ctx.current_user)):
        return public_user(user)

    @router.get("/api/users")
    def list_users(_: dict[str, Any] = Depends(ctx.require_admin)):
        return [public_user(item) for item in ctx.repos.users.list_with_assignments()]

    @router.post("/api/users")
    def create_user(body: UserCreate, _: dict[str, Any] = Depends(ctx.require_admin)):
        username = body.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        if ctx.repos.users.username_exists(username):
            raise HTTPException(status_code=409, detail="username already exists")
        validate_user_password(body.password)
        if body.role not in {"admin", "service"}:
            raise HTTPException(status_code=400, detail="role must be admin or service")
        try:
            user_id = ctx.repos.users.create(
                username=username,
                password_hash=hash_password(body.password),
                display_name=body.display_name,
                role=body.role,
                is_active=body.is_active,
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="username already exists") from exc
        ctx.repos.users.assign_shops(user_id, body.shop_ids)
        return ctx.repos.users.by_id_public(user_id)

    @router.patch("/api/users/{user_id}")
    def update_user(user_id: int, body: UserUpdate, _: dict[str, Any] = Depends(ctx.require_admin)):
        target = ctx.repos.users.by_id(user_id)
        if not target:
            raise HTTPException(status_code=404, detail="user not found")
        assignments: dict[str, Any] = {}
        for name in ("display_name", "is_active"):
            value = getattr(body, name)
            if value is None:
                continue
            assignments[name] = int(value) if name == "is_active" else value
        if body.password and body.password.strip():
            validate_user_password(body.password.strip())
            assignments["password_hash"] = hash_password(body.password.strip())
        if assignments:
            ctx.repos.users.update_user(user_id, assignments)
            ctx.hub.disconnect_user(user_id)
        user = ctx.repos.users.by_id_public(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        return public_user(user)

    return router
