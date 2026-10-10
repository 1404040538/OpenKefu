"""Conversations, messages and reply/transfer/intent/action attempt records data access."""

from __future__ import annotations

from typing import Any

from openkefu.web.db import Database, json_dumps

CONVERSATION_SELECT = """
    SELECT c.*, s.name AS shop_name, s.status AS shop_status,
           s.auto_reply_enabled AS shop_auto_reply_enabled
    FROM conversations c
    JOIN shops s ON s.id=c.shop_id
"""
CONVERSATION_ORDER = """
    ORDER BY c.human_attention_required DESC,
             c.human_attention_at DESC,
             c.updated_at DESC
"""


class ConversationsRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    def by_id_plain(self, conversation_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM conversations WHERE id=%s", (conversation_id,))

    def row(self, conversation_id: int) -> dict[str, Any] | None:
        return self.db.query_one(f"{CONVERSATION_SELECT} WHERE c.id=%s", (conversation_id,))

    def list_for_shop(self, shop_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            {CONVERSATION_SELECT}
            WHERE c.shop_id=%s AND c.user_uid <> 'unknown'
              AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
            {CONVERSATION_ORDER}
            LIMIT %s OFFSET %s
            """,
            (shop_id, limit, offset),
        )

    def list_all_admin(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            {CONVERSATION_SELECT}
            WHERE c.user_uid <> 'unknown'
              AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
            {CONVERSATION_ORDER}
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )

    def list_all_for_user(self, user_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            {CONVERSATION_SELECT}
            JOIN shop_assignments sa ON sa.shop_id=s.id
            WHERE sa.user_id=%s AND c.user_uid <> 'unknown'
              AND s.mall_id IS NOT NULL AND c.mall_id <=> s.mall_id
            {CONVERSATION_ORDER}
            LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        )

    def shop_mall_row(self, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT mall_id FROM shops WHERE id=%s", (shop_id,))

    def shop_mall_id(self, shop_id: int) -> str:
        row = self.db.query_one("SELECT mall_id FROM shops WHERE id=%s", (shop_id,))
        return str((row or {}).get("mall_id") or "")

    def reset_bot_reply_for_shop(self, shop_id: int) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET bot_reply_enabled=1,
                human_attention_required=0,
                human_attention_reason=NULL,
                human_attention_at=NULL
            WHERE shop_id=%s
            """,
            (shop_id,),
        )

    def set_bot_reply(self, conversation_id: int, enabled: bool) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET bot_reply_enabled=%s,
                human_attention_required=IF(%s=1, 0, human_attention_required),
                human_attention_reason=IF(%s=1, NULL, human_attention_reason),
                human_attention_at=IF(%s=1, NULL, human_attention_at)
            WHERE id=%s
            """,
            (int(enabled), int(enabled), int(enabled), int(enabled), conversation_id),
        )

    def clear_attention(self, conversation_id: int) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET human_attention_required=0,
                human_attention_reason=NULL,
                human_attention_at=NULL
            WHERE id=%s
            """,
            (conversation_id,),
        )

    def update_fields(self, conversation_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(
            f"UPDATE conversations SET {fields} WHERE id=%s",
            [*assignments.values(), conversation_id],
        )

    def create(self, *, shop_id: int, mall_id: str, user_uid: str, **extra: Any) -> int:
        columns = {"shop_id": shop_id, "mall_id": mall_id, "user_uid": user_uid, **extra}
        names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        return self.db.execute(
            f"INSERT INTO conversations ({names}) VALUES ({placeholders})",
            list(columns.values()),
        )

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------
    def messages_page(self, conversation_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.db.query(
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

    def insert_message(self, assignments: dict[str, Any]) -> int:
        names = ", ".join(assignments)
        placeholders = ", ".join(["%s"] * len(assignments))
        return self.db.execute(
            f"INSERT INTO messages ({names}) VALUES ({placeholders})",
            list(assignments.values()),
        )

    def update_message(self, message_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE messages SET {fields} WHERE id=%s", [*assignments.values(), message_id])

    def message_by_shop_msg_id(self, shop_id: int, msg_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM messages WHERE shop_id=%s AND msg_id=%s",
            (shop_id, msg_id),
        )

    def recent_messages(self, conversation_id: int, *, limit: int) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE conversation_id=%s
                ORDER BY id DESC
                LIMIT %s
            ) recent_messages
            ORDER BY id ASC
            """,
            (conversation_id, limit),
        )

    def latest_customer_message_time(self, conversation_id: int) -> Any:
        row = self.db.query_one(
            """
            SELECT created_at FROM messages
            WHERE conversation_id=%s AND direction='incoming'
            ORDER BY id DESC LIMIT 1
            """,
            (conversation_id,),
        )
        return (row or {}).get("created_at")

    # ------------------------------------------------------------------
    # attempt records (reply / transfer / intent / action)
    # ------------------------------------------------------------------
    def insert_row(self, table: str, assignments: dict[str, Any]) -> int:
        names = ", ".join(assignments)
        placeholders = ", ".join(["%s"] * len(assignments))
        return self.db.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            list(assignments.values()),
        )

    def update_row(self, table: str, row_id: int, assignments: dict[str, Any]) -> None:
        if not assignments:
            return
        fields = ", ".join(f"{name}=%s" for name in assignments)
        self.db.execute(f"UPDATE {table} SET {fields} WHERE id=%s", [*assignments.values(), row_id])

    def row_by_id(self, table: str, row_id: int) -> dict[str, Any] | None:
        return self.db.query_one(f"SELECT * FROM {table} WHERE id=%s", (row_id,))

    # 常用封装：意图事件与动作请求
    def store_intent_event(self, assignments: dict[str, Any]) -> int:
        fixed = {
            "shop_id": assignments.get("shop_id"),
            "conversation_id": assignments.get("conversation_id"),
            "message_id": assignments.get("message_id"),
            "user_uid": assignments.get("user_uid"),
            "intent": assignments.get("intent"),
            "slots": json_dumps(assignments.get("slots") or {}),
            "actions": json_dumps(assignments.get("actions") or []),
        }
        return self.insert_row("intent_events", {k: v for k, v in fixed.items() if v is not None})

    def cleanup_old_attempts(self, *, days: int) -> dict[str, int]:
        """Delete expired intent/action/reply/transfer attempt rows."""
        results: dict[str, int] = {}
        for table in ("intent_events", "action_requests", "reply_attempts", "transfer_attempts"):
            results[table] = self.db.execute(
                f"DELETE FROM {table} WHERE created_at < DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
            )
        return results

    # ------------------------------------------------------------------
    # shop_runner message ingestion
    # ------------------------------------------------------------------
    def by_shop_uid_mall(self, shop_id: int, user_uid: str, mall_id: str | None) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM conversations WHERE shop_id=%s AND user_uid=%s AND mall_id <=> %s",
            (shop_id, user_uid, mall_id),
        )

    def touch_unknown_event(self, conversation_id: int, *, mall_id: str | None, preview: str) -> None:
        self.db.execute(
            "UPDATE conversations SET mall_id=%s, last_message_preview=%s, unread_count=unread_count+1 WHERE id=%s",
            (mall_id, preview, conversation_id),
        )

    def create_unknown_event_conversation(self, *, shop_id: int, mall_id: str | None, user_uid: str, preview: str) -> int:
        return self.db.execute(
            """
            INSERT INTO conversations (shop_id, mall_id, conv_id, user_uid, last_message_preview, unread_count)
            VALUES (%s,%s,%s,%s,%s,1)
            """,
            (shop_id, mall_id, user_uid, user_uid, preview),
        )

    def insert_unknown_event_message(self, *, shop_id: int, conversation_id: int, user_uid: str, preview: str, raw_json: str) -> int:
        return self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, user_uid, sender_role, kind, content, raw_json, status)
            VALUES (%s,%s,'system',%s,'system','unknown',%s,%s,'received')
            """,
            (shop_id, conversation_id, user_uid, preview, raw_json),
        )

    def touch_on_message(self, conversation_id: int, params: dict[str, Any]) -> None:
        """条件更新会话的最新消息预览/时间/未读数（SQL 原样自 shop_runner）。"""
        self.db.execute(
            """
            UPDATE conversations
            SET mall_id=%s, conv_id=%s, chat_type_id=%s, chat_type=%s, nickname=%s,
                last_message_preview=IF(last_message_at IS NULL OR last_message_at<=%s, %s, last_message_preview),
                last_message_at=IF(last_message_at IS NULL OR last_message_at<=%s, %s, last_message_at),
                unread_count=unread_count+%s
            WHERE id=%s
            """,
            (
                params["mall_id"], params["conv_id"], params["chat_type_id"], params["chat_type"],
                params["nickname"], params["message_at"], params["preview"],
                params["message_at"], params["message_at"], params["unread_delta"], conversation_id,
            ),
        )

    def create_from_incoming(self, params: dict[str, Any]) -> int:
        return self.db.execute(
            """
            INSERT INTO conversations
            (shop_id, mall_id, conv_id, chat_type_id, chat_type, user_uid, nickname,
             last_message_preview, last_message_at, unread_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                params["shop_id"], params["mall_id"], params["conv_id"], params["chat_type_id"],
                params["chat_type"], params["user_uid"], params["nickname"],
                params["preview"], params["message_at"], params["unread_delta"],
            ),
        )

    def insert_chat_message(self, params: dict[str, Any]) -> int:
        return self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, msg_id, client_msg_id, user_uid,
             sender_role, message_type, kind, content, goods_json, size_json, raw_json, message_at, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                params["shop_id"], params["conversation_id"], params["direction"],
                params["msg_id"], params["client_msg_id"], params["user_uid"],
                params["sender_role"], params["message_type"], params["kind"], params["content"],
                params.get("goods_json"), params.get("size_json"), params["raw_json"], params["message_at"],
                params.get("status") or "received",
            ],
        )

    def existing_message_row(self, shop_id: int, *, msg_id: str | None = None, client_msg_id: str | None = None) -> dict[str, Any] | None:
        if msg_id:
            row = self.db.query_one(
                "SELECT id, conversation_id FROM messages WHERE shop_id=%s AND msg_id=%s LIMIT 1",
                (shop_id, msg_id),
            )
            if row:
                return row
        if client_msg_id:
            row = self.db.query_one(
                "SELECT id, conversation_id FROM messages WHERE shop_id=%s AND client_msg_id=%s LIMIT 1",
                (shop_id, client_msg_id),
            )
            if row:
                return row
        return None

    def bot_reply_enabled(self, conversation_id: int, shop_id: int) -> bool:
        row = self.db.query_one(
            "SELECT bot_reply_enabled FROM conversations WHERE id=%s AND shop_id=%s",
            (conversation_id, shop_id),
        )
        return bool((row or {}).get("bot_reply_enabled"))

    def backfill_mall_id(self, shop_id: int, mall_id: str) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET mall_id=%s
            WHERE shop_id=%s AND (mall_id IS NULL OR mall_id='')
            """,
            (mall_id, shop_id),
        )

    def has_outbound(self, conversation_id: int) -> bool:
        row = self.db.query_one(
            "SELECT COUNT(*) AS cnt FROM messages WHERE conversation_id=%s AND direction='outbound'",
            (conversation_id,),
        )
        return bool(row and row.get("cnt", 0) > 0)

    def insert_intent_event(self, *, shop_id: int, conversation_id: int, message_id: int | None,
                            user_uid: str, intent: dict[str, Any], knowledge_hits: list[dict[str, Any]]) -> int:
        return self.db.execute(
            """
            INSERT INTO intent_events
            (shop_id, conversation_id, message_id, user_uid, intent_code, raw_intent_code,
             confidence, resolution_status, reply, slots_json, actions_json, knowledge_json,
             raw_json, status, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'replied',%s)
            """,
            (
                shop_id, conversation_id, message_id, user_uid,
                intent.get("intent_code") or "unknown",
                intent.get("raw_intent_code") or intent.get("intent_code") or "unknown",
                float(intent.get("confidence") or 0),
                intent.get("resolution_status") or "need_human",
                intent.get("reply") or "",
                json_dumps(intent.get("slots") or {}),
                json_dumps(intent.get("actions") or []),
                json_dumps(knowledge_hits or []),
                json_dumps(intent.get("raw") or intent),
                intent.get("error") or None,
            ),
        )

    def insert_reply_attempt(self, *, shop_id: int, conversation_id: int, message_id: int | None,
                             user_uid: str, content: str) -> int:
        return self.db.execute(
            """
            INSERT INTO reply_attempts
            (shop_id, conversation_id, message_id, user_uid, content, status)
            VALUES (%s,%s,%s,%s,%s,'sending')
            """,
            (shop_id, conversation_id, message_id, user_uid, content),
        )

    def insert_outbound_message(self, *, shop_id: int, conversation_id: int,
                                user_uid: str, kind: str, content: str) -> int:
        return self.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, user_uid, sender_role, kind, content, status)
            VALUES (%s,%s,'outbound',%s,'service',%s,%s,'sending')
            """,
            (shop_id, conversation_id, user_uid, kind, content),
        )

    def insert_transfer_attempt(self, *, shop_id: int, conversation_id: int,
                                user_uid: str, csid: str, remark: str) -> int:
        return self.db.execute(
            """
            INSERT INTO transfer_attempts
            (shop_id, conversation_id, user_uid, csid, remark, status)
            VALUES (%s,%s,%s,%s,%s,'sending')
            """,
            (shop_id, conversation_id, user_uid, csid, remark),
        )

    def mark_transferred(self, conversation_id: int) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET transferred_at=NOW(),
                bot_reply_enabled=0,
                human_attention_required=0,
                human_attention_reason=NULL,
                human_attention_at=NULL
            WHERE id=%s
            """,
            (conversation_id,),
        )

    def message_raw_json(self, message_id: int, shop_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT raw_json FROM messages WHERE id=%s AND shop_id=%s",
            (message_id, shop_id),
        )

    def raw_json_history(self, conversation_id: int, *, limit: int = 80) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            SELECT id, raw_json, message_at, created_at
            FROM messages
            WHERE conversation_id=%s AND raw_json IS NOT NULL
            ORDER BY id DESC
            LIMIT {int(limit)}
            """,
            (conversation_id,),
        )

    def set_bot_reply_for_shop(self, conversation_id: int, shop_id: int, enabled: bool) -> None:
        self.db.execute(
            "UPDATE conversations SET bot_reply_enabled=%s WHERE id=%s AND shop_id=%s",
            (int(enabled), conversation_id, shop_id),
        )

    def mark_human_attention(self, conversation_id: int, shop_id: int, reason: str) -> None:
        self.db.execute(
            """
            UPDATE conversations
            SET bot_reply_enabled=0,
                human_attention_required=1,
                human_attention_reason=%s,
                human_attention_at=NOW()
            WHERE id=%s AND shop_id=%s
            """,
            (reason, conversation_id, shop_id),
        )

    # ------------------------------------------------------------------
    # LLM history helpers (sort expression keeps message_at semantics)
    # ------------------------------------------------------------------
    _SORT_EXPR = (
        "CASE "
        "WHEN message_at IS NULL THEN UNIX_TIMESTAMP(created_at) * 1000 "
        "WHEN message_at < 100000000000 THEN message_at * 1000 "
        "ELSE message_at END"
    )

    def message_sort_at(self, message_id: int) -> int | None:
        row = self.db.query_one(
            f"SELECT {self._SORT_EXPR} AS sort_at FROM messages WHERE id=%s",
            (message_id,),
        )
        return int(row["sort_at"]) if row and row.get("sort_at") is not None else None

    def llm_history_rows(self, *, conversation_id: int, after_message_id: int,
                         cutoff: int, current_message_id: int, limit: int) -> list[dict[str, Any]]:
        return self.db.query(
            f"""
            SELECT direction, kind, content, goods_json, raw_json
            FROM messages
            WHERE conversation_id=%s
              AND id>%s
              AND direction IN ('inbound','outbound')
              AND (
                {self._SORT_EXPR} < %s
                OR ({self._SORT_EXPR} = %s AND id <= %s)
              )
            ORDER BY {self._SORT_EXPR} DESC, id DESC
            LIMIT {int(limit)}
            """,
            (conversation_id, after_message_id, cutoff, cutoff, current_message_id),
        )

    def latest_transfer_boundary(self, conversation_id: int, current_message_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            """
            SELECT ie.message_id AS source_message_id,
                   (
                     SELECT MIN(m.id)
                     FROM messages m
                     WHERE m.conversation_id=ie.conversation_id
                       AND m.id>ie.message_id
                       AND m.direction='outbound'
                   ) AS transfer_reply_message_id
            FROM intent_events ie
            WHERE ie.conversation_id=%s
              AND ie.message_id<%s
              AND ie.actions_json LIKE %s
            ORDER BY ie.id DESC
            LIMIT 1
            """,
            (conversation_id, current_message_id, "%transfer_to_human%"),
        )
