"""Runtime worker routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from openkefu.web.context import AppContext


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runtime/workers")
    def runtime_workers(user: dict[str, Any] = Depends(ctx.require_admin)):
        return ctx.runtime.runtime_workers()

    @router.get("/api/runtime/shops/{shop_id}/owner")
    def runtime_shop_owner(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        owner = ctx.runtime.shop_owner(shop_id)
        if not owner:
            raise HTTPException(status_code=404, detail="runtime owner not found")
        return owner

    return router
