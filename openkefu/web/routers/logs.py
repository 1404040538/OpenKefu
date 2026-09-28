"""Runtime log query routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from openkefu.web.context import AppContext
from openkefu.web.db import Database
from openkefu.web.realtime import sanitize_log_value


def build_router(ctx: AppContext) -> APIRouter:
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
        return query_runtime_logs(
            ctx.db,
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


def query_runtime_logs(
    db: Database,
    *,
    user: dict[str, Any],
    id: int | None,
    level: str | None,
    module: str | None,
    action: str | None,
    keyword: str | None,
    shop_id: int | None,
    user_uid: str | None,
    conversation_id: int | None,
    request_id: str | None,
    run_id: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    where: list[str] = []
    values: list[Any] = []

    if user["role"] != "admin":
        assigned = [
            int(row["shop_id"])
            for row in db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user["id"],))
        ]
        if assigned:
            where.append(f"(shop_id IS NULL OR shop_id IN ({','.join(['%s'] * len(assigned))}))")
            values.extend(assigned)
        else:
            where.append("shop_id IS NULL")

    if id is not None:
        where.append("id=%s")
        values.append(id)
    if level:
        levels = [item.strip().upper() for item in level.split(",") if item.strip()]
        if levels:
            where.append(f"level IN ({','.join(['%s'] * len(levels))})")
            values.extend(levels)
    if module:
        where.append("module LIKE %s")
        values.append(f"%{module.strip()}%")
    if action:
        where.append("action LIKE %s")
        values.append(f"%{action.strip()}%")
    if keyword:
        like = f"%{keyword.strip()}%"
        where.append("(message LIKE %s OR error_trace LIKE %s OR context_json LIKE %s)")
        values.extend([like, like, like])
    if shop_id:
        where.append("shop_id=%s")
        values.append(shop_id)
    if user_uid:
        where.append("user_uid LIKE %s")
        values.append(f"%{user_uid.strip()}%")
    if conversation_id:
        where.append("conversation_id=%s")
        values.append(conversation_id)
    if request_id:
        where.append("request_id LIKE %s")
        values.append(f"%{request_id.strip()}%")
    if run_id:
        where.append("run_id LIKE %s")
        values.append(f"%{run_id.strip()}%")
    if date_from:
        where.append("created_at >= %s")
        values.append(date_from)
    if date_to:
        where.append("created_at <= %s")
        values.append(date_to)

    where_sql = " WHERE " + " AND ".join(where) if where else ""
    total_row = db.query_one(f"SELECT COUNT(*) AS cnt FROM runtime_logs{where_sql}", values)
    items = db.query(
        f"""
        SELECT id, level, module, action, message, shop_id, mall_id, conversation_id,
               user_uid, request_id, run_id, pid, error_trace, context_json, created_at
        FROM runtime_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT %s OFFSET %s
        """,
        [*values, limit, offset],
    )
    return {
        "items": [_format_runtime_log(row) for row in items],
        "total": int(total_row["cnt"] if total_row else 0),
        "limit": limit,
        "offset": offset,
        "facets": _runtime_log_facets(db, user),
    }


def _format_runtime_log(row: dict[str, Any]) -> dict[str, Any]:
    try:
        context = json.loads(row.get("context_json") or "{}")
    except (TypeError, ValueError):
        context = {}
    return {
        **row,
        "context": sanitize_log_value(context),
        "created_at": row.get("created_at").isoformat(sep=" ", timespec="seconds") if row.get("created_at") else "",
    }


def _runtime_log_facets(db: Database, user: dict[str, Any]) -> dict[str, Any]:
    where = ""
    values: list[Any] = []
    if user["role"] != "admin":
        assigned = [
            int(row["shop_id"])
            for row in db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user["id"],))
        ]
        if assigned:
            where = f"WHERE shop_id IS NULL OR shop_id IN ({','.join(['%s'] * len(assigned))})"
            values.extend(assigned)
        else:
            where = "WHERE shop_id IS NULL"
    levels = db.query(f"SELECT level, COUNT(*) AS count FROM runtime_logs {where} GROUP BY level ORDER BY level", values)
    modules = db.query(f"SELECT module, COUNT(*) AS count FROM runtime_logs {where} GROUP BY module ORDER BY count DESC LIMIT 30", values)
    actions = db.query(f"SELECT action, COUNT(*) AS count FROM runtime_logs {where} GROUP BY action ORDER BY count DESC LIMIT 50", values)
    runs = db.query(
        f"""
        SELECT run_id, MIN(created_at) AS started_at, MAX(created_at) AS ended_at, COUNT(*) AS count
        FROM runtime_logs
        {where}
        GROUP BY run_id
        ORDER BY MAX(created_at) DESC
        LIMIT 30
        """,
        values,
    )
    return {
        "levels": levels,
        "modules": modules,
        "actions": actions,
        "runs": [
            {
                **row,
                "started_at": row.get("started_at").isoformat(sep=" ", timespec="seconds") if row.get("started_at") else "",
                "ended_at": row.get("ended_at").isoformat(sep=" ", timespec="seconds") if row.get("ended_at") else "",
            }
            for row in runs
        ],
    }
