"""Authentication and platform-setting routes."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymysql.err import IntegrityError

from openkefu.web.context import DEFAULT_REGISTER_LLM_REPLIES, DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES, DEFAULT_SERVICE_MAX_SHOPS, AppContext
from openkefu.web.deps import client_ip, is_loopback_ip, validate_user_password
from openkefu.web.security import hash_password, verify_password


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class SetupAdminRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class AdminSettingsUpdate(BaseModel):
    registration_enabled: bool | None = None


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/status")
    def auth_status():
        return {
            "initialized": bool(ctx.db.query_one("SELECT id FROM users LIMIT 1")),
            "registration_enabled": ctx.registration_enabled(),
        }

    @router.post("/api/auth/setup-admin")
    def setup_admin(body: SetupAdminRequest, request: Request):
        ip = client_ip(request)
        whitelist = {item.strip() for item in os.getenv("OPENKEFU_SETUP_ADMIN_IP_WHITELIST", "").split(",") if item.strip()}
        if not whitelist and not is_loopback_ip(ip):
            raise HTTPException(status_code=403, detail="setup admin is only allowed from localhost")
        if whitelist and ip not in whitelist:
            raise HTTPException(status_code=403, detail="setup admin ip not allowed")
        if not ctx.setup_admin_limiter.hit(f"setup-admin:{ip}"):
            raise HTTPException(status_code=429, detail="too many setup attempts, try again later")
        username = body.username.strip()
        password = body.password
        display_name = body.display_name.strip() or username
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        validate_user_password(password)
        # 原子化初始化：对 app_settings 加行锁串行化并发请求，
        # 避免"未初始化窗口"被并发双请求抢建两个 admin。
        with ctx.db.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT value_json FROM app_settings WHERE `key`='registration_enabled' FOR UPDATE")
                cursor.execute("SELECT id FROM users LIMIT 1")
                if cursor.fetchone():
                    raise HTTPException(status_code=409, detail="system already initialized")
                cursor.execute(
                    """
                    INSERT INTO users (username, password_hash, display_name, role, is_active)
                    VALUES (%s, %s, %s, 'admin', 1)
                    """,
                    (username, hash_password(password), display_name),
                )
                user_id = int(cursor.lastrowid)
        user = ctx.db.query_one("SELECT * FROM users WHERE id=%s", (user_id,))
        if not user:
            raise HTTPException(status_code=500, detail="admin user created but not found")
        return ctx.auth_response(user)

    @router.post("/api/auth/login")
    def login(body: LoginRequest, request: Request):
        ip = client_ip(request)
        if not ctx.login_limiter.hit(f"login:{ip}:{body.username}"):
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
        user = ctx.db.query_one("SELECT * FROM users WHERE username=%s", (body.username,))
        if not user or not user.get("is_active") or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="invalid username or password")
        return ctx.auth_response(user)

    @router.post("/api/auth/register")
    def register(body: RegisterRequest, request: Request):
        if not ctx.register_limiter.hit(f"register:{client_ip(request)}"):
            raise HTTPException(status_code=429, detail="注册尝试过于频繁，请稍后再试")
        if not ctx.db.query_one("SELECT id FROM users LIMIT 1"):
            raise HTTPException(status_code=409, detail="system is not initialized")
        if not ctx.registration_enabled():
            raise HTTPException(status_code=403, detail="注册功能已关闭")
        username = body.username.strip()
        password = body.password
        display_name = body.display_name.strip() or username
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        validate_user_password(password)
        if ctx.db.query_one("SELECT id FROM users WHERE username=%s", (username,)):
            raise HTTPException(status_code=409, detail="username already exists")
        try:
            user_id = ctx.db.execute(
                """
                INSERT INTO users
                (username, password_hash, display_name, role, is_active,
                 max_shops, max_knowledge_bases, max_llm_replies)
                VALUES (%s,%s,%s,'service',1,%s,%s,%s)
                """,
                (
                    username,
                    hash_password(password),
                    display_name,
                    DEFAULT_SERVICE_MAX_SHOPS,
                    DEFAULT_SERVICE_MAX_KNOWLEDGE_BASES,
                    DEFAULT_REGISTER_LLM_REPLIES,
                ),
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="username already exists") from exc
        user = ctx.db.query_one("SELECT * FROM users WHERE id=%s", (user_id,))
        if not user:
            raise HTTPException(status_code=500, detail="registered user created but not found")
        return ctx.auth_response(user)

    @router.get("/api/admin/settings")
    def get_admin_settings(_: dict[str, Any] = Depends(ctx.require_admin)):
        return {"registration_enabled": ctx.registration_enabled()}

    @router.patch("/api/admin/settings")
    def update_admin_settings(body: AdminSettingsUpdate, _: dict[str, Any] = Depends(ctx.require_admin)):
        if body.registration_enabled is not None:
            ctx.set_setting("registration_enabled", bool(body.registration_enabled))
        return {"registration_enabled": ctx.registration_enabled()}

    return router
