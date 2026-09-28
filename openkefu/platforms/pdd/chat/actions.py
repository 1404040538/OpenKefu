from __future__ import annotations

from typing import Any

from openkefu.web.db import Database, json_dumps
from openkefu.web.realtime import RealtimeHub, RuntimeLogger


ACTION_DESCRIPTIONS = {
    "small_payment": "小额打款接口预留",
    "payment_reminder": "催款接口预留",
    "shipping_change": "快递/地址变更接口预留",
    "order_remark": "订单备注接口预留",
    "return_exchange": "退换货接口预留",
    "invite_review": "邀评接口预留",
    "express_exception_followup": "快递异常跟进接口预留",
    "feedback_record": "反馈登记接口预留",
    "transfer_to_human": "转人工接口预留",
}


class ActionDispatcher:
    def __init__(self, db: Database, hub: RealtimeHub, runtime_logger: RuntimeLogger):
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger

    def create_and_dispatch(
        self,
        *,
        shop_id: int,
        conversation_id: int,
        message_id: int,
        intent_event_id: int | None,
        user_uid: str,
        actions: list[dict[str, Any]],
        slots: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for action in actions:
            action_type = str(action.get("type") or "transfer_to_human")
            payload = dict(action.get("payload") or {})
            if slots:
                payload.setdefault("slots", slots)
            action_id = self.db.execute(
                """
                INSERT INTO action_requests
                (shop_id, conversation_id, message_id, intent_event_id, user_uid, action_type, payload_json, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
                """,
                (
                    shop_id,
                    conversation_id,
                    message_id,
                    intent_event_id,
                    user_uid,
                    action_type,
                    json_dumps(payload),
                ),
            )
            rows.append(self._dispatch_noop(action_id))
        return rows

    def _dispatch_noop(self, action_id: int) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM action_requests WHERE id=%s", (action_id,))
        if not row:
            raise ValueError(f"action request not found: {action_id}")
        result = {
            "reserved": True,
            "message": ACTION_DESCRIPTIONS.get(row["action_type"], "动作接口预留"),
        }
        self.db.execute(
            "UPDATE action_requests SET status='noop', result_json=%s WHERE id=%s",
            (json_dumps(result), action_id),
        )
        updated = self.db.query_one("SELECT * FROM action_requests WHERE id=%s", (action_id,))
        self.hub.publish({"type": "action_request", "data": updated})
        self.runtime_logger.log(
            "INFO",
            __name__,
            "action.noop",
            result["message"],
            shop_id=int(row["shop_id"]),
            conversation_id=row.get("conversation_id"),
            user_uid=row.get("user_uid"),
            request_id=str(action_id),
            context={"action_type": row["action_type"], "has_payload": bool(row.get("payload_json"))},
        )
        return updated or row
