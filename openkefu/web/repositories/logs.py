"""Runtime log persistence and query data access."""

from __future__ import annotations

import json
from typing import Any

from openkefu.web.db import Database
from openkefu.web.realtime import sanitize_log_value

LOG_INSERT_SQL = """
    INSERT INTO runtime_logs
    (level, module, action, message, shop_id, mall_id, conversation_id,
     user_uid, request_id, run_id, pid, error_trace, context_json)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


class RuntimeLogsRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_batch(self, batch: list[tuple[Any, ...]]) -> None:
        if not batch:
            return
        self.db.execute_many(LOG_INSERT_SQL, batch)

    def delete_old(self, *, days: int) -> int:
        return self.db.execute(
            f"DELETE FROM runtime_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
        )

    # ------------------------------------------------------------------
    # query API
    # ------------------------------------------------------------------
    def query(
        self,
        *,
        user: dict[str, Any],
        id: int | None = None,
        level: str | None = None,
        module: str | None = None,
        action: str | None = None,
        keyword: str | None = None,
        shop_id: int | None = None,
        user_uid: str | None = None,
        conversation_id: int | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        values: list[Any] = []

        if user["role"] != "admin":
            assigned = [
                int(row["shop_id"])
                for row in self.db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user["id"],))
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
        total_row = self.db.query_one(f"SELECT COUNT(*) AS cnt FROM runtime_logs{where_sql}", values)
        items = self.db.query(
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
            "items": [self.format_row(row) for row in items],
            "total": int(total_row["cnt"] if total_row else 0),
            "limit": limit,
            "offset": offset,
            "facets": self.facets(user),
        }

    @staticmethod
    def format_row(row: dict[str, Any]) -> dict[str, Any]:
        try:
            context = json.loads(row.get("context_json") or "{}")
        except (TypeError, ValueError):
            context = {}
        return {
            **row,
            "context": sanitize_log_value(context),
            "created_at": row.get("created_at").isoformat(sep=" ", timespec="seconds") if row.get("created_at") else "",
        }

    def facets(self, user: dict[str, Any]) -> dict[str, Any]:
        where = ""
        values: list[Any] = []
        if user["role"] != "admin":
            assigned = [
                int(row["shop_id"])
                for row in self.db.query("SELECT shop_id FROM shop_assignments WHERE user_id=%s", (user["id"],))
            ]
            if assigned:
                where = f"WHERE shop_id IS NULL OR shop_id IN ({','.join(['%s'] * len(assigned))})"
                values.extend(assigned)
            else:
                where = "WHERE shop_id IS NULL"
        levels = self.db.query(f"SELECT level, COUNT(*) AS count FROM runtime_logs {where} GROUP BY level ORDER BY level", values)
        modules = self.db.query(f"SELECT module, COUNT(*) AS count FROM runtime_logs {where} GROUP BY module ORDER BY count DESC LIMIT 30", values)
        actions = self.db.query(f"SELECT action, COUNT(*) AS count FROM runtime_logs {where} GROUP BY action ORDER BY count DESC LIMIT 50", values)
        runs = self.db.query(
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
