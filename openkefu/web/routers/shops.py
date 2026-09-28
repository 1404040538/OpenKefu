"""Shop lifecycle routes: CRUD, login flows, notes, QR codes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from openkefu.platforms.pdd.chat.llm import get_llm_client
from openkefu.web.config import AppConfig
from openkefu.web.context import AppContext
from openkefu.web.crypto import TextCipher
from openkefu.web.db import json_dumps


class ShopCreate(BaseModel):
    name: str
    remark: str = ""
    auto_reply_enabled: bool = True
    transfer_csids: list[str] = []
    greeting_message: str | None = None
    greeting_use_llm: bool = False
    force_ai_reply: bool = False


class ShopUpdate(BaseModel):
    name: str | None = None
    remark: str | None = None
    auto_reply_enabled: bool | None = None
    transfer_csids: list[str] | None = None
    greeting_message: str | None = None
    greeting_use_llm: bool | None = None
    force_ai_reply: bool | None = None


class OptimizeMessageRequest(BaseModel):
    message: str
    msg_type: str = "greeting"


class NoteCreate(BaseModel):
    content: str


class NoteUpdate(BaseModel):
    content: str


class PasswordLoginRequest(BaseModel):
    username: str
    password: str


class LoginCredentialsSave(BaseModel):
    username: str
    password: str


class LoginCredentialsClear(BaseModel):
    pass


def _optimize_text(raw: str, config: AppConfig) -> str:
    llm = get_llm_client(config.llm)
    system_prompt = (
        "你是一个专业的电商客服开场白优化助手。请把用户提供的开场白优化得更亲切、自然、专业，"
        "适合在顾客首次咨询时发送。保持原意，不要改成问题答复，不要添加不存在的承诺或优惠。"
        "只输出优化后的开场白，不要加解释或标记。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": raw},
    ]
    try:
        result = llm.chat(messages, max_tokens=512, temperature=0.3)
        return result.strip() or raw
    except Exception:
        return raw


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shops")
    def list_shops(user: dict[str, Any] = Depends(ctx.current_user)):
        rows = (ctx.repos.shops.list_with_cache_admin() if user["role"] == "admin"
                else ctx.repos.shops.list_with_cache_for_user(int(user["id"])))
        return ctx.normalize_shop_runtime_status(rows)

    @router.post("/api/shops")
    def create_shop(body: ShopCreate, user: dict[str, Any] = Depends(ctx.current_user)):
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="shop name is required")

        shop_id = ctx.repos.shops.create(
            name=name,
            remark=body.remark,
            auto_reply_enabled=body.auto_reply_enabled,
            transfer_csids=body.transfer_csids,
            created_by=int(user["id"]),
            greeting_message=body.greeting_message,
            greeting_use_llm=body.greeting_use_llm,
            force_ai_reply=body.force_ai_reply,
        )
        return ctx.repos.shops.by_id(shop_id)

    @router.patch("/api/shops/{shop_id}")
    def update_shop(shop_id: int, body: ShopUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        old_auto_reply = ctx.repos.shops.get_auto_reply_enabled(shop_id)
        assignments: dict[str, Any] = {}
        bool_fields = {"auto_reply_enabled", "greeting_use_llm", "force_ai_reply"}
        for name in ("name", "remark", "auto_reply_enabled", "transfer_csids",
                     "greeting_message", "greeting_use_llm", "force_ai_reply"):
            value = getattr(body, name)
            if value is None:
                continue
            if name in bool_fields:
                assignments[name] = int(value)
            elif name == "transfer_csids":
                assignments[name] = json_dumps(value) if value else None
            else:
                assignments[name] = value
        if assignments:
            ctx.repos.shops.update_fields(shop_id, assignments)
            if body.auto_reply_enabled is True and not old_auto_reply:
                ctx.repos.conversations.reset_bot_reply_for_shop(shop_id)
        shop = ctx.repos.shops.row_with_cache(shop_id)
        ctx.hub.publish({"type": "shop_status", "data": shop})
        return shop

    @router.post("/api/shops/{shop_id}/start")
    def start_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        try:
            return ctx.runtime_call("start_shop", shop_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/login")
    def login_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        try:
            return ctx.runtime_call("login_shop", shop_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/password-login")
    def password_login_shop(shop_id: int, body: PasswordLoginRequest, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        try:
            return ctx.runtime_call("password_login_shop", shop_id, {"username": body.username, "password": body.password})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/login-credentials")
    def save_login_credentials(shop_id: int, body: LoginCredentialsSave, user: dict[str, Any] = Depends(ctx.current_user)):
        """保存账密（加密存储），用于登录态过期时自动重新登录。"""
        ctx.can_access_shop(user, shop_id)
        if not ctx.db.query_one("SELECT id FROM shops WHERE id=%s", (shop_id,)):
            raise HTTPException(status_code=404, detail="shop not found")
        username = body.username.strip()
        password = body.password.strip()
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password are required")
        cipher = TextCipher(ctx.config.security.data_encryption_key)
        ctx.repos.shops.save_login_credentials(
            shop_id, cipher.encrypt(username), cipher.encrypt(password),
        )
        return {"ok": True, "auto_relogin_enabled": True}

    @router.delete("/api/shops/{shop_id}/login-credentials")
    def clear_login_credentials(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        """清除保存的账密，停止自动续登。"""
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        ctx.repos.shops.clear_login_credentials(shop_id)
        return {"ok": True, "auto_relogin_enabled": False}

    @router.get("/api/shops/{shop_id}/login-credentials-status")
    def login_credentials_status(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        """查询是否已启用自动续登（不返回任何账密内容）。"""
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        return {"auto_relogin_enabled": ctx.repos.shops.has_login_credentials(shop_id)}

    @router.post("/api/shops/{shop_id}/password-login/send-sms")
    def password_login_send_sms(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        try:
            ok = ctx.runtime_call("password_login_send_sms", shop_id)
            return {"success": ok}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/password-login/verify")
    async def password_login_verify(shop_id: int, request: Request, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                verify_code = body.get("verify_code") or body.get("code") or ""
            else:
                verify_code = body or ""
            verify_code = str(verify_code).strip()
            if not verify_code:
                raise HTTPException(status_code=400, detail="验证码不能为空")
            return await asyncio.to_thread(ctx.runtime_call, "password_login_verify", shop_id, {"verify_code": verify_code})
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/online")
    def online_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        try:
            return ctx.runtime_call("online_shop", shop_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/shops/{shop_id}/offline")
    def offline_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        try:
            ctx.runtime_call("offline_shop", shop_id, timeout=10)
        except Exception:
            ctx.repos.shops.set_status(shop_id, "offline")
        return {"ok": True}

    @router.post("/api/shops/{shop_id}/stop")
    def stop_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        try:
            ctx.runtime_call("stop_shop", shop_id, timeout=10)
        except Exception:
            ctx.repos.shops.set_status(shop_id, "stopped")
        return {"ok": True}

    @router.delete("/api/shops/{shop_id}")
    def delete_shop(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        shop = ctx.repos.shops.by_id(shop_id)
        if not shop:
            raise HTTPException(status_code=404, detail="shop not found")
        if user["role"] != "admin":
            if shop.get("created_by_user_id") != user["id"]:
                raise HTTPException(status_code=403, detail="只能删除自己创建的店铺")
        try:
            ctx.runtime_call("offline_shop", shop_id, timeout=10)
        except Exception:
            ctx.repos.shops.set_status(shop_id, "offline")
        ctx.repos.shops.delete_shop_chat_records(shop_id)
        ctx.repos.shops.delete(shop_id)
        return {"ok": True}

    @router.post("/api/shops/{shop_id}/optimize-message")
    def optimize_shop_message(shop_id: int, body: OptimizeMessageRequest, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        if body.msg_type != "greeting":
            raise HTTPException(status_code=400, detail="only greeting optimization is supported")
        raw = body.message.strip()
        if not raw:
            raise HTTPException(status_code=400, detail="message is empty")
        try:
            optimized = _optimize_text(raw, ctx.config)
        except Exception:
            raise HTTPException(status_code=502, detail="大模型优化失败，请稍后再试")
        ctx.repos.shops.set_greeting_message(shop_id, optimized)
        return {"optimized": optimized, "column": "greeting_message"}

    @router.get("/api/shops/{shop_id}/notes")
    def list_shop_notes(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        return ctx.repos.shops.notes(shop_id)

    @router.post("/api/shops/{shop_id}/notes")
    def create_shop_note(shop_id: int, body: NoteCreate, user: dict[str, Any] = Depends(ctx.require_admin)):
        if not ctx.repos.shops.exists(shop_id):
            raise HTTPException(status_code=404, detail="shop not found")
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        note_id = ctx.repos.shops.create_note(shop_id, content, int(user["id"]))
        return ctx.repos.shops.note_by_id(note_id)

    @router.patch("/api/shops/{shop_id}/notes/{note_id}")
    def update_shop_note(shop_id: int, note_id: int, body: NoteUpdate, user: dict[str, Any] = Depends(ctx.require_admin)):
        if not ctx.repos.shops.note_exists(note_id, shop_id):
            raise HTTPException(status_code=404, detail="note not found")
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        ctx.repos.shops.update_note(note_id, content)
        return ctx.repos.shops.note_by_id(note_id)

    @router.delete("/api/shops/{shop_id}/notes/{note_id}")
    def delete_shop_note(shop_id: int, note_id: int, user: dict[str, Any] = Depends(ctx.require_admin)):
        if not ctx.repos.shops.note_exists(note_id, shop_id):
            raise HTTPException(status_code=404, detail="note not found")
        ctx.repos.shops.delete_note(note_id)
        return {"ok": True}

    @router.get("/api/shops/{shop_id}/qrcode")
    def shop_qrcode(shop_id: int, attempt_id: int | None = Query(default=None), user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        row = ctx.repos.shops.qr_attempt(shop_id=shop_id, attempt_id=attempt_id)
        if not row or not Path(row["qrcode_path"]).exists():
            raise HTTPException(status_code=404, detail="qrcode not found")
        return FileResponse(row["qrcode_path"], media_type="image/png")

    return router
