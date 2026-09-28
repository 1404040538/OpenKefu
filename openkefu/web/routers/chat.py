"""Conversation routes: listings, messages, replies, transfers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from openkefu.web.context import AppContext

DEFAULT_MAX_REPLY_IMAGE_BYTES = 10 * 1024 * 1024


class ReplyRequest(BaseModel):
    content: str


class ReplyImageRequest(BaseModel):
    image_base64: str
    image_name: str | None = None


class TransferRequest(BaseModel):
    csid: str
    remark: str = "无原因直接转移"


class ConversationBotReplyUpdate(BaseModel):
    enabled: bool


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shops/{shop_id}/conversations")
    def conversations(
        shop_id: int,
        user: dict[str, Any] = Depends(ctx.current_user),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        ctx.can_access_shop(user, shop_id)
        return ctx.repos.conversations.list_for_shop(shop_id, limit=limit, offset=offset)

    @router.get("/api/conversations")
    def all_conversations(
        user: dict[str, Any] = Depends(ctx.current_user),
        shop_id: int | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        if shop_id:
            ctx.can_access_shop(user, shop_id)
            return ctx.repos.conversations.list_for_shop(shop_id, limit=limit, offset=offset)
        if user["role"] == "admin":
            return ctx.repos.conversations.list_all_admin(limit=limit, offset=offset)
        return ctx.repos.conversations.list_all_for_user(int(user["id"]), limit=limit, offset=offset)

    @router.get("/api/shops/{shop_id}/transfer-services")
    def transfer_services(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        try:
            return ctx.runtime_call("transfer_services", shop_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/conversations/{conversation_id}/messages")
    def messages(
        conversation_id: int,
        user: dict[str, Any] = Depends(ctx.current_user),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        return ctx.repos.conversations.messages_page(conversation_id, limit=limit, offset=offset)

    @router.get("/api/conversations/{conversation_id}/context")
    def conversation_context(conversation_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        try:
            return ctx.runtime_call("conversation_context", int(conversation["shop_id"]), {"conversation_id": conversation_id})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/conversations/{conversation_id}/reply")
    def reply(conversation_id: int, body: ReplyRequest, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        try:
            return ctx.runtime_call("send_reply", int(conversation["shop_id"]), {"conversation_id": conversation_id, "content": body.content})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/conversations/{conversation_id}/reply-image")
    def reply_image(conversation_id: int, body: ReplyImageRequest, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        image_base64 = body.image_base64.strip()
        if not image_base64.startswith("data:image/"):
            raise HTTPException(status_code=400, detail="invalid image data")
        try:
            image_data = image_base64.split(",", 1)[1] if "," in image_base64 else image_base64
            if len(image_data.encode("utf-8")) > DEFAULT_MAX_REPLY_IMAGE_BYTES:
                raise HTTPException(status_code=400, detail="图片大小不能超过 10MB")
        except HTTPException:
            raise
        try:
            return ctx.runtime_call("send_image", int(conversation["shop_id"]), {"conversation_id": conversation_id, "image_base64": image_base64})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/conversations/{conversation_id}/transfer")
    def transfer(conversation_id: int, body: TransferRequest, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        try:
            return ctx.runtime_call("transfer", int(conversation["shop_id"]), {"conversation_id": conversation_id, "csid": body.csid, "remark": body.remark})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/conversations/{conversation_id}/bot-reply")
    def update_conversation_bot_reply(conversation_id: int, body: ConversationBotReplyUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        ctx.repos.conversations.set_bot_reply(conversation_id, body.enabled)
        ctx.publish_conversation(conversation_id)
        return ctx.repos.conversations.row(conversation_id)

    @router.post("/api/conversations/{conversation_id}/clear-attention")
    def clear_conversation_attention(conversation_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.repos.conversations.by_id_plain(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        ctx.repos.conversations.clear_attention(conversation_id)
        ctx.publish_conversation(conversation_id)
        return ctx.repos.conversations.row(conversation_id)

    return router
