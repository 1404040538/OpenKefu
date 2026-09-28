"""Runtime worker and server status routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from openkefu.web.context import AppContext
from openkefu.web.server_status import latest_server_status, server_status_history


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runtime/workers")
    def runtime_workers(user: dict[str, Any] = Depends(ctx.require_admin)):
        return ctx.runtime.runtime_workers()

    @router.get("/api/server-status/latest")
    def server_status_latest(user: dict[str, Any] = Depends(ctx.require_admin)):
        return latest_server_status(ctx.db)

    @router.get("/api/server-status/history")
    def server_status_history_api(
        range: str = Query(default="1h", pattern="^(1h|6h|24h|7d|30d)$"),
        user: dict[str, Any] = Depends(ctx.require_admin),
    ):
        return server_status_history(ctx.db, range)

    @router.get("/api/runtime/shops/{shop_id}/owner")
    def runtime_shop_owner(shop_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        ctx.can_access_shop(user, shop_id)
        owner = ctx.runtime.shop_owner(shop_id)
        if not owner:
            raise HTTPException(status_code=404, detail="runtime owner not found")
        return owner

    return router
