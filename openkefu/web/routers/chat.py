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
        return ctx.db.query(
            f"""
            {ctx.conversation_select_sql()}
            WHERE c.shop_id=%s AND c.user_uid <> 'unknown'
              AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
            {ctx.conversation_order_sql()}
            LIMIT %s OFFSET %s
            """,
            (shop_id, limit, offset),
        )

    @router.get("/api/conversations")
    def all_conversations(
        user: dict[str, Any] = Depends(ctx.current_user),
        shop_id: int | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        if shop_id:
            ctx.can_access_shop(user, shop_id)
            return ctx.db.query(
                f"""
                {ctx.conversation_select_sql()}
                WHERE c.shop_id=%s AND c.user_uid <> 'unknown'
                  AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
                {ctx.conversation_order_sql()}
                LIMIT %s OFFSET %s
                """,
                (shop_id, limit, offset),
            )
        if user["role"] == "admin":
            return ctx.db.query(
                f"""
                {ctx.conversation_select_sql()}
                WHERE c.user_uid <> 'unknown'
                  AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
                {ctx.conversation_order_sql()}
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
        return ctx.db.query(
            f"""
            {ctx.conversation_select_sql()}
            JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE sa.user_id=%s AND c.user_uid <> 'unknown'
              AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
            {ctx.conversation_order_sql()}
            LIMIT %s OFFSET %s
            """,
            (user["id"], limit, offset),
        )

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
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        return ctx.db.query(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE conversation_id=%s
                  AND NOT (direction='system' AND COALESCE(content, '')='')
                ORDER BY id DESC
                LIMIT %s OFFSET %s
            ) recent_messages
            ORDER BY id ASC
            """,
            (conversation_id, limit, offset),
        )

    @router.get("/api/conversations/{conversation_id}/context")
    def conversation_context(conversation_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
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
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
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
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
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
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
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
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        ctx.db.execute(
            """
            UPDATE conversations
            SET bot_reply_enabled=%s,
                human_attention_required=IF(%s=1, 0, human_attention_required),
                human_attention_reason=IF(%s=1, NULL, human_attention_reason),
                human_attention_at=IF(%s=1, NULL, human_attention_at)
            WHERE id=%s
            """,
            (int(body.enabled), int(body.enabled), int(body.enabled), int(body.enabled), conversation_id),
        )
        ctx.publish_conversation(conversation_id)
        return ctx.conversation_row(conversation_id)

    @router.post("/api/conversations/{conversation_id}/clear-attention")
    def clear_conversation_attention(conversation_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        conversation = ctx.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        ctx.can_access_shop(user, int(conversation["shop_id"]))
        ctx.ensure_current_conversation(conversation)
        ctx.db.execute(
            """
            UPDATE conversations
            SET human_attention_required=0,
                human_attention_reason=NULL,
                human_attention_at=NULL
            WHERE id=%s
            """,
            (conversation_id,),
        )
        ctx.publish_conversation(conversation_id)
        return ctx.conversation_row(conversation_id)

    return router
