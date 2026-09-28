"""Return/exchange records data access."""

from __future__ import annotations

from typing import Any

from openkefu.web.db import Database

RECORD_WITH_SHOP_SELECT = """
    SELECT rr.*, s.name AS shop_name
    FROM return_records rr
    JOIN shops s ON s.id=rr.shop_id
"""
RECORD_FIELDS = [
    "shop_id", "conversation_id", "message_id", "user_uid", "username", "order_no",
    "order_status", "record_type", "new_address", "remark", "source_message",
]


class ReturnRecordsRepository:
    def __init__(self, db: Database):
        self.db = db

    def count(self, where_sql: str, values: list[Any]) -> int:
        row = self.db.query_one(f"SELECT COUNT(*) AS cnt FROM return_records rr{where_sql}", values)
        return int((row or {}).get("cnt") or 0)

    def search(self, where_sql: str, values: list[Any], *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else "LIMIT 5000"
        return self.db.query(
            f"""
            {RECORD_WITH_SHOP_SELECT}
            {where_sql}
            ORDER BY rr.created_at DESC, rr.id DESC
            {limit_clause} OFFSET {int(offset)}
            """,
            values,
        )

    def by_id(self, record_id: int) -> dict[str, Any] | None:
        return self.db.query_one(f"{RECORD_WITH_SHOP_SELECT} WHERE rr.id=%s", (record_id,))

    def find_duplicate(self, *, shop_id: int, user_uid: str, order_no: str, exclude_id: int | None = None) -> dict[str, Any] | None:
        rows = self.db.query(
            """
            SELECT id FROM return_records
            WHERE shop_id=%s AND user_uid=%s AND order_no=%s
            ORDER BY id DESC
            """,
            (shop_id, user_uid, order_no),
        )
        for row in rows:
            if exclude_id is None or int(row["id"]) != int(exclude_id):
                return row
        return None

    def update(self, record_id: int, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{key}=%s" for key in RECORD_FIELDS) + ", slots_json=NULL, order_snapshot_json=NULL, created_at=NOW()"
        self.db.execute(
            f"UPDATE return_records SET {assignments} WHERE id=%s",
            [values.get(key) for key in RECORD_FIELDS] + [record_id],
        )

    def delete(self, record_id: int) -> None:
        self.db.execute("DELETE FROM return_records WHERE id=%s", (record_id,))

    def insert(self, values: dict[str, Any]) -> int:
        placeholders = ", ".join(["%s"] * len(RECORD_FIELDS))
        return self.db.execute(
            f"""
            INSERT INTO return_records
            (shop_id, conversation_id, message_id, user_uid, username, order_no, order_status,
             record_type, new_address, remark, source_message, slots_json, order_snapshot_json)
            VALUES ({placeholders},NULL,NULL)
            """,
            [values.get(key) for key in RECORD_FIELDS],
        )

    def upsert(self, data: dict[str, Any], *, existing_id: int | None = None) -> dict[str, Any]:
        existing: dict[str, Any] | None = None
        if data.get("shop_id") and data.get("user_uid") and data.get("order_no"):
            existing = self.find_duplicate(
                shop_id=int(data["shop_id"]),
                user_uid=str(data["user_uid"]),
                order_no=str(data["order_no"]),
                exclude_id=existing_id,
            )
        target_id = int(existing["id"]) if existing else existing_id
        values = {key: data.get(key) for key in RECORD_FIELDS}
        values["username"] = values.get("username") or values.get("user_uid") or ""
        values["order_status"] = values.get("order_status") or "待核实"
        if target_id:
            self.update(int(target_id), values)
            if existing and existing_id and int(existing["id"]) != int(existing_id):
                self.delete(int(existing_id))
            return self.by_id(int(target_id)) or {}
        record_id = self.insert(values)
        return self.by_id(record_id) or {}
