"""Knowledge base and note-set routes."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024


def validate_upload_size(data: bytes) -> None:
    if len(data) > MAX_UPLOAD_FILE_BYTES:
        raise HTTPException(status_code=400, detail="文件大小不能超过 20MB")


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str = ""
    shop_ids: list[int] = []


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    shop_ids: list[int] | None = None


class KnowledgeQaItem(BaseModel):
    question: str
    reply: str | None = None
    image_base64: str | None = None
    image_name: str | None = None
    image_content_type: str | None = None


class KnowledgeQaItemsUpdate(BaseModel):
    items: list[KnowledgeQaItem] = []


class NoteItemCreate(BaseModel):
    content: str


class NoteItemUpdate(BaseModel):
    content: str


async def read_single_upload(request: Request) -> tuple[str, str, bytes]:
    content_type = request.headers.get("content-type") or ""
    boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not boundary_match:
        raise HTTPException(status_code=400, detail="missing multipart boundary")
    boundary = ("--" + boundary_match.group(1)).encode()
    body = await request.body()
    for raw_part in body.split(boundary):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_bytes, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = header_bytes.decode("utf-8", errors="replace")
        if 'name="file"' not in headers:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers)
        filename = filename_match.group(1) if filename_match else "upload"
        part_type_match = re.search(r"content-type:\s*([^\r\n]+)", headers, flags=re.IGNORECASE)
        part_type = part_type_match.group(1).strip() if part_type_match else ""
        return filename, part_type, data.rstrip(b"\r\n")
    raise HTTPException(status_code=400, detail="missing file field")


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/api/knowledge-bases")
    def knowledge_bases(user: dict[str, Any] = Depends(ctx.current_user)):
        rows = ctx.knowledge.list_knowledge_bases()
        if user["role"] == "admin":
            return rows
        return [row for row in rows if int(row.get("created_by") or 0) == int(user["id"])]

    @router.post("/api/knowledge-bases")
    def create_knowledge_base(body: KnowledgeBaseCreate, user: dict[str, Any] = Depends(ctx.current_user)):
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="knowledge base name is required")
        ctx.ensure_shop_ids_manageable(user, body.shop_ids)
        return ctx.knowledge.create_knowledge_base(
            name=body.name,
            description=body.description,
            shop_ids=body.shop_ids,
            created_by=int(user["id"]),
        )

    @router.get("/api/knowledge-bases/{kb_id}")
    def get_knowledge_base(kb_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        try:
            row = ctx.knowledge.get_knowledge_base(kb_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not ctx.can_manage_owned_row(user, row):
            raise HTTPException(status_code=403, detail="knowledge base belongs to another user")
        return row

    @router.patch("/api/knowledge-bases/{kb_id}")
    def update_knowledge_base(kb_id: int, body: KnowledgeBaseUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_knowledge_base_manageable(kb_id, user)
        if body.shop_ids is not None:
            ctx.ensure_shop_ids_manageable(user, body.shop_ids)
        try:
            return ctx.knowledge.update_knowledge_base(
                kb_id,
                name=body.name,
                description=body.description,
                status=body.status,
                shop_ids=body.shop_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/knowledge-bases/{kb_id}")
    def delete_knowledge_base(kb_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_knowledge_base_manageable(kb_id, user)
        try:
            ctx.knowledge.delete_knowledge_base(kb_id)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/knowledge-bases/{kb_id}/files")
    async def upload_knowledge_file(kb_id: int, request: Request, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_knowledge_base_manageable(kb_id, user)
        filename, content_type, data = await read_single_upload(request)
        validate_upload_size(data)
        try:
            return await asyncio.to_thread(
                ctx.knowledge.upload_file_bytes,
                kb_id=kb_id,
                filename=filename,
                content_type=content_type,
                data=data,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/knowledge-bases/{kb_id}/qa-items")
    def update_knowledge_qa_items(kb_id: int, body: KnowledgeQaItemsUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_knowledge_base_manageable(kb_id, user)
        try:
            return ctx.knowledge.set_qa_items(
                kb_id,
                [item.model_dump() for item in body.items],
                created_by=int(user["id"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/note-sets")
    def note_sets(user: dict[str, Any] = Depends(ctx.current_user)):
        rows = ctx.note_service.list_note_sets()
        if user["role"] == "admin":
            return rows
        return [row for row in rows if int(row.get("created_by") or 0) == int(user["id"])]

    @router.post("/api/note-sets")
    def create_note_set(body: KnowledgeBaseCreate, user: dict[str, Any] = Depends(ctx.current_user)):
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="note set name is required")
        ctx.ensure_shop_ids_manageable(user, body.shop_ids)
        return ctx.note_service.create_note_set(
            name=body.name,
            description=body.description,
            shop_ids=body.shop_ids,
            created_by=int(user["id"]),
        )

    @router.get("/api/note-sets/{ns_id}")
    def get_note_set(ns_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        try:
            row = ctx.note_service.get_note_set(ns_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not ctx.can_manage_owned_row(user, row):
            raise HTTPException(status_code=403, detail="note set belongs to another user")
        return row

    @router.patch("/api/note-sets/{ns_id}")
    def update_note_set(ns_id: int, body: KnowledgeBaseUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_note_set_manageable(ns_id, user)
        if body.shop_ids is not None:
            ctx.ensure_shop_ids_manageable(user, body.shop_ids)
        try:
            return ctx.note_service.update_note_set(
                ns_id,
                name=body.name,
                description=body.description,
                status=body.status,
                shop_ids=body.shop_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/note-sets/{ns_id}")
    def delete_note_set(ns_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_note_set_manageable(ns_id, user)
        try:
            ctx.note_service.delete_note_set(ns_id)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/note-sets/{ns_id}/items")
    def add_note_set_item(ns_id: int, body: NoteItemCreate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_note_set_manageable(ns_id, user)
        if not body.content.strip():
            raise HTTPException(status_code=400, detail="note content is required")
        return ctx.note_service.add_item(ns_id, body.content, int(user["id"]))

    @router.patch("/api/note-sets/{ns_id}/items/{item_id}")
    def update_note_set_item(ns_id: int, item_id: int, body: NoteItemUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_note_set_manageable(ns_id, user)
        if not body.content.strip():
            raise HTTPException(status_code=400, detail="note content is required")
        if not ctx.db.query_one("SELECT id FROM note_set_items WHERE id=%s AND note_set_id=%s", (item_id, ns_id)):
            raise HTTPException(status_code=404, detail="note item not found")
        return ctx.note_service.update_item(item_id, body.content)

    @router.delete("/api/note-sets/{ns_id}/items/{item_id}")
    def delete_note_set_item(ns_id: int, item_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.ensure_note_set_manageable(ns_id, user)
        if not ctx.db.query_one("SELECT id FROM note_set_items WHERE id=%s AND note_set_id=%s", (item_id, ns_id)):
            raise HTTPException(status_code=404, detail="note item not found")
        ctx.note_service.delete_item(item_id)
        return {"ok": True}

    return router
