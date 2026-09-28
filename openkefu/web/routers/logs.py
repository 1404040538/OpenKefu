"""Runtime log query routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/api/logs")
    def logs(
        user: dict[str, Any] = Depends(ctx.require_admin),
        id: int | None = Query(default=None),
        level: str | None = Query(default=None),
        module: str | None = Query(default=None),
        action: str | None = Query(default=None),
        keyword: str | None = Query(default=None),
        shop_id: int | None = Query(default=None),
        user_uid: str | None = Query(default=None),
        conversation_id: int | None = Query(default=None),
        request_id: str | None = Query(default=None),
        run_id: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
        limit: int = Query(default=100),
        offset: int = Query(default=0),
    ):
        if shop_id:
            ctx.can_access_shop(user, shop_id)
        return ctx.repos.logs.query(
            user=user,
            id=id,
            level=level,
            module=module,
            action=action,
            keyword=keyword,
            shop_id=shop_id,
            user_uid=user_uid,
            conversation_id=conversation_id,
            request_id=request_id,
            run_id=run_id,
            date_from=date_from,
            date_to=date_to,
            limit=min(max(limit, 20), 500),
            offset=max(offset, 0),
        )

    return router
