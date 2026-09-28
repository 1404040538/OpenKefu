from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from openkefu.web.db import Database, json_dumps
from openkefu.web.realtime import RealtimeHub, RuntimeLogger


RECORD_TYPE_LABELS = {
    "return": "退货",
    "exchange": "换货",
    "address_change": "修改地址",
    "refund_only": "仅退款",
    "logistics_intercept": "物流拦截",
    "other": "其他",
}

RECORD_ACTIONS = {
    "return_exchange",
    "shipping_change",
    "order_remark",
    "express_exception_followup",
}

ORDER_REQUIRED_RECORD_TYPES = {
    "return",
    "exchange",
    "address_change",
    "refund_only",
    "logistics_intercept",
    "other",
}

SLOT_ALIASES = {
    "order_no": ("order_no", "order_sn", "orderNo", "orderSn", "order_id", "orderId", "订单号"),
    "new_address": ("new_address", "address", "receiver_address", "shipping_address", "收货地址", "新地址"),
    "remark": ("remark", "note", "reason", "description", "备注", "原因"),
}


@dataclass
class ReturnRecordResult:
    handled: bool
    reply: str
    intent: dict[str, Any]
    record: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None


class ReturnRecordService:
    def __init__(self, db: Database, hub: RealtimeHub, runtime_logger: RuntimeLogger):
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger

    def handle_intent(
        self,
        *,
        shop_id: int,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        user_content: str,
        intent: dict[str, Any],
        order_lookup: Callable[[str], dict[str, Any]],
    ) -> ReturnRecordResult | None:
        draft = self._draft(shop_id, conversation_id)
        record_type = self._classify(intent, user_content, draft)
        if not record_type:
            return None

        conversation = self.db.query_one("SELECT nickname FROM conversations WHERE id=%s", (conversation_id,)) or {}
        username = str(conversation.get("nickname") or user_uid or "").strip()
        slots = self._merge_slots(draft, intent, user_content, record_type)
        order_no = self._clean_text(slots.get("order_no"))
        if not order_no and draft:
            order_no = self._order_no_from_draft_orders(user_content, draft)
            if order_no:
                slots["order_no"] = order_no
        new_address = self._clean_text(slots.get("new_address"))
        remark = self._clean_text(slots.get("remark"))
        source_message = self._clean_text(user_content)

        order_snapshot = self._load_order_snapshot(order_no, order_lookup)
        if not order_no:
            order_no = self._order_no_from_snapshot(order_snapshot)
            if order_no:
                slots["order_no"] = order_no
        if self._order_lookup_says_not_found(order_snapshot):
            reply = "亲，这边没有查询到您的订单记录，暂时无法为您登记该订单的处理需求。请您确认是否已在本店下单。"
            sanitized = self._sanitize_intent(intent, record_type, complete=False)
            sanitized["reply"] = reply
            sanitized["resolution_status"] = "resolved"
            return ReturnRecordResult(handled=True, reply=reply, intent=sanitized)
        order_status = self._order_status_from_snapshot(order_snapshot)
        missing_fields = self._missing_fields(record_type, username, order_no, new_address, remark)
        if self._order_lookup_is_ambiguous(order_snapshot) and "order_no" not in missing_fields:
            missing_fields.insert(0, "order_no")

        payload = {
            "username": username,
            "record_type": record_type,
            "order_no": order_no,
            "order_status": order_status,
            "new_address": new_address,
            "remark": remark,
            "source_message": source_message,
            "slots": slots,
            "order_snapshot": order_snapshot,
            "missing_fields": missing_fields,
        }

        sanitized = self._sanitize_intent(intent, record_type, complete=not missing_fields)
        if missing_fields:
            draft_row = self._upsert_draft(
                shop_id=shop_id,
                conversation_id=conversation_id,
                user_uid=user_uid,
                payload=payload,
            )
            reply = self._missing_reply(record_type, missing_fields, order_snapshot)
            sanitized["reply"] = reply
            sanitized["resolution_status"] = "need_more_info"
            return ReturnRecordResult(handled=True, reply=reply, intent=sanitized, draft=draft_row)

        record = self._create_record(
            shop_id=shop_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_uid=user_uid,
            payload=payload,
        )
        self.db.execute("DELETE FROM return_record_drafts WHERE shop_id=%s AND conversation_id=%s", (shop_id, conversation_id))
        reply = self._complete_reply(record_type, payload)
        sanitized["reply"] = reply
        sanitized["resolution_status"] = "resolved"
        self.hub.publish({"type": "return_record", "data": record})
        self.runtime_logger.log(
            "INFO",
            __name__,
            "return_record.created",
            f"created {record_type} return record",
            shop_id=shop_id,
            conversation_id=conversation_id,
            user_uid=user_uid,
            request_id=str(record.get("id")),
            context={"record_type": record_type, "has_order_no": bool(order_no), "order_status": order_status},
        )
        return ReturnRecordResult(handled=True, reply=reply, intent=sanitized, record=record)

    def _draft(self, shop_id: int, conversation_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM return_record_drafts WHERE shop_id=%s AND conversation_id=%s",
            (shop_id, conversation_id),
        )

    def _classify(self, intent: dict[str, Any], text: str, draft: dict[str, Any] | None) -> str:
        if draft and draft.get("record_type"):
            if self._message_updates_draft(text, draft, intent):
                return str(draft["record_type"])
            return ""

        normalized = str(text or "")
        if self._looks_like_plain_consultation(normalized):
            return ""

        intent_code = str(intent.get("intent_code") or "")
        actions = {
            str(item.get("type") or "")
            for item in intent.get("actions") or []
            if isinstance(item, dict)
        }

        if self._looks_like_address_change(normalized):
            return "address_change"
        if self._looks_like_logistics_intercept(normalized):
            return "logistics_intercept"
        if any(keyword in normalized for keyword in ("仅退款", "只退款", "退钱", "退款")) and "退货" not in normalized:
            return "refund_only"
        if any(keyword in normalized for keyword in ("换货", "调换", "换一个", "换一件")):
            return "exchange"
        if any(keyword in normalized for keyword in ("退货", "退换货", "七天无理由")):
            return "return"

        if intent_code == "shipping_change" or "shipping_change" in actions:
            return "address_change"
        if intent_code in {"return_exchange", "seven_day_no_reason"} or "return_exchange" in actions:
            return "return"
        if (intent_code == "express_exception" or "express_exception_followup" in actions) and self._looks_like_recordable_express_issue(normalized):
            return "logistics_intercept"
        if intent_code == "order_remark" or actions.intersection(RECORD_ACTIONS):
            if self._looks_like_other_record_request(normalized):
                return "other"
        if intent_code == "custom" and any(keyword in normalized for keyword in ("取消订单", "订单备注", "备注订单", "帮我备注")):
            return "other"
        return ""

    @staticmethod
    def _looks_like_plain_consultation(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        if ReturnRecordService._looks_like_record_operation_request(normalized):
            return False
        consultation_keywords = (
            "发什么快递",
            "发啥快递",
            "什么快递",
            "哪个快递",
            "哪家快递",
            "用什么快递",
            "用哪家快递",
            "发哪家",
            "什么物流",
            "哪个物流",
            "快递公司",
            "什么时候发货",
            "多久发货",
            "发货了吗",
            "发货没",
            "几天发货",
            "包邮吗",
            "运费",
        )
        if any(keyword in normalized for keyword in consultation_keywords):
            return True
        return any(marker in normalized for marker in ("吗", "么", "嘛", "?", "？")) and any(
            keyword in normalized for keyword in ("快递", "物流", "发货", "运费", "包邮")
        )

    @staticmethod
    def _looks_like_record_operation_request(text: str) -> bool:
        normalized = str(text or "")
        operation_keywords = (
            "拦截",
            "截回",
            "召回",
            "不要发货",
            "别发货",
            "停止发货",
            "取消订单",
            "退款",
            "退钱",
            "仅退款",
            "只退款",
            "退货",
            "退换货",
            "七天无理由",
            "换货",
            "调换",
            "改地址",
            "修改地址",
            "更改地址",
            "换地址",
            "地址改",
            "订单备注",
            "备注订单",
            "帮我备注",
            "登记",
            "处理一下",
            "帮我处理",
            "售后",
        )
        return any(keyword in normalized for keyword in operation_keywords)

    @staticmethod
    def _looks_like_logistics_intercept(text: str) -> bool:
        normalized = str(text or "")
        return any(keyword in normalized for keyword in ("拦截", "截回", "召回", "不要发货", "别发货", "停止发货"))

    @staticmethod
    def _looks_like_recordable_express_issue(text: str) -> bool:
        normalized = str(text or "")
        issue_keywords = (
            "拦截",
            "截回",
            "召回",
            "退回",
            "拒收",
            "丢件",
            "丢了",
            "破损",
            "损坏",
            "异常",
            "卡住",
            "停滞",
            "不动",
            "没更新",
            "没收到",
            "未收到",
            "少件",
            "错发",
            "漏发",
            "派错",
        )
        return any(keyword in normalized for keyword in issue_keywords)

    @staticmethod
    def _looks_like_other_record_request(text: str) -> bool:
        normalized = str(text or "")
        if ReturnRecordService._looks_like_plain_consultation(normalized):
            return False
        return any(keyword in normalized for keyword in ("取消订单", "订单备注", "备注订单", "帮我备注", "帮忙备注", "登记", "处理一下", "帮我处理", "售后"))

    def _merge_slots(self, draft: dict[str, Any] | None, intent: dict[str, Any], text: str, record_type: str) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        if draft and draft.get("slots_json"):
            try:
                slots.update(json.loads(draft["slots_json"]) or {})
            except (TypeError, ValueError):
                pass
            for key in ("order_no", "new_address", "remark"):
                if draft.get(key):
                    slots[key] = draft[key]

        raw_slots = intent.get("slots") if isinstance(intent.get("slots"), dict) else {}
        slots.update({key: value for key, value in raw_slots.items() if value not in (None, "")})

        for canonical, aliases in SLOT_ALIASES.items():
            if slots.get(canonical):
                continue
            for alias in aliases:
                if raw_slots.get(alias):
                    slots[canonical] = raw_slots[alias]
                    break

        order_no = self._extract_order_no(text)
        if order_no:
            slots["order_no"] = order_no

        if record_type == "address_change" and not self._clean_text(slots.get("new_address")):
            address = self._extract_address(text)
            if address:
                slots["new_address"] = address
        if record_type == "other" and not self._clean_text(slots.get("remark")):
            remark = self._extract_remark(text)
            if remark:
                slots["remark"] = remark
        return slots

    def _message_updates_draft(self, text: str, draft: dict[str, Any], intent: dict[str, Any]) -> bool:
        text = str(text or "").strip()
        record_type = str(draft.get("record_type") or "")
        if record_type == "address_change" and (
            self._looks_like_address_change(text) or self._extract_address(text)
        ):
            return True

        missing = self._draft_missing_fields(draft)
        if not missing:
            return False

        raw_slots = intent.get("slots") if isinstance(intent.get("slots"), dict) else {}
        for field in missing:
            if any(raw_slots.get(alias) for alias in SLOT_ALIASES.get(field, ())):
                return True

        if "order_no" in missing and self._extract_order_no(text):
            return True
        if "order_no" in missing and self._order_no_from_draft_orders(text, draft):
            return True
        if "new_address" in missing and self._extract_address(text):
            return True
        if "remark" in missing and self._extract_remark(text):
            return True

        return False

    @staticmethod
    def _draft_missing_fields(draft: dict[str, Any]) -> list[str]:
        try:
            value = json.loads(draft.get("missing_fields_json") or "[]")
        except (TypeError, ValueError):
            value = []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @staticmethod
    def _order_no_from_draft_orders(text: str, draft: dict[str, Any]) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        try:
            snapshot = json.loads(draft.get("order_snapshot_json") or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        orders = snapshot.get("orders") if isinstance(snapshot, dict) else None
        if not isinstance(orders, list):
            return ""
        lowered = text.lower()
        selected_index = ReturnRecordService._selected_order_index(text)
        if selected_index is not None and 0 <= selected_index < len(orders):
            order = orders[selected_index]
            if isinstance(order, dict):
                return ReturnRecordService._order_no_from_snapshot({"order": order})
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_no = ReturnRecordService._order_no_from_snapshot({"order": order})
            goods_name = ReturnRecordService._first_goods_name(order)
            if order_no and order_no.lower() in lowered:
                return order_no
            if goods_name and (goods_name in text or text in goods_name):
                return order_no
        return ""

    @staticmethod
    def _selected_order_index(text: str) -> int | None:
        normalized = str(text or "").strip().lower()
        match = re.search(r"(?:第\s*)?([1-5])\s*(?:个|单|件|号)?", normalized)
        if match:
            return int(match.group(1)) - 1
        chinese = {"一": 0, "二": 1, "两": 1, "三": 2, "四": 3, "五": 4}
        for word, index in chinese.items():
            if f"第{word}" in normalized or f"{word}个" in normalized or f"{word}单" in normalized:
                return index
        return None

    def _load_order_snapshot(self, order_no: str, order_lookup: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = order_lookup(order_no)
        except Exception as exc:
            return {"status": "failed", "order_status": "查询失败", "error": str(exc)}
        if not isinstance(result, dict):
            return {"status": "failed", "order_status": "查询失败", "raw_response": result}
        return result

    @staticmethod
    def _order_status_from_snapshot(snapshot: dict[str, Any]) -> str:
        return str(snapshot.get("order_status") or snapshot.get("status_label") or snapshot.get("status") or "待核实")

    @staticmethod
    def _order_no_from_snapshot(snapshot: dict[str, Any]) -> str:
        order = snapshot.get("order") if isinstance(snapshot.get("order"), dict) else {}
        candidates = (
            snapshot.get("order_no"),
            snapshot.get("order_sn"),
            snapshot.get("orderSn"),
            order.get("order_no"),
            order.get("orderNo"),
            order.get("order_sn"),
            order.get("orderSn"),
            order.get("order_id"),
            order.get("orderId"),
        )
        return next((str(value).strip() for value in candidates if str(value or "").strip()), "")

    @staticmethod
    def _order_lookup_says_not_found(snapshot: dict[str, Any]) -> bool:
        status = str(snapshot.get("status") or "").lower()
        return status in {"not_found", "empty", "no_record", "no_order"}

    @staticmethod
    def _order_lookup_is_ambiguous(snapshot: dict[str, Any]) -> bool:
        return str(snapshot.get("status") or "").lower() in {"ambiguous", "multiple"}

    @staticmethod
    def _missing_fields(record_type: str, username: str, order_no: str, new_address: str, remark: str) -> list[str]:
        missing: list[str] = []
        if not username:
            missing.append("username")
        if record_type in ORDER_REQUIRED_RECORD_TYPES and not order_no:
            missing.append("order_no")
        if record_type == "address_change" and not new_address:
            missing.append("new_address")
        if record_type == "other" and not remark:
            missing.append("remark")
        return missing

    def _upsert_draft(self, *, shop_id: int, conversation_id: int, user_uid: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.db.execute(
            """
            INSERT INTO return_record_drafts
            (shop_id, conversation_id, user_uid, username, record_type, order_no, order_status,
             new_address, remark, source_message, slots_json, order_snapshot_json, missing_fields_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              user_uid=VALUES(user_uid),
              username=VALUES(username),
              record_type=VALUES(record_type),
              order_no=VALUES(order_no),
              order_status=VALUES(order_status),
              new_address=VALUES(new_address),
              remark=VALUES(remark),
              source_message=VALUES(source_message),
              slots_json=VALUES(slots_json),
              order_snapshot_json=VALUES(order_snapshot_json),
              missing_fields_json=VALUES(missing_fields_json)
            """,
            (
                shop_id,
                conversation_id,
                user_uid,
                payload["username"],
                payload["record_type"],
                payload["order_no"],
                payload["order_status"],
                payload["new_address"] or None,
                payload["remark"] or None,
                payload["source_message"] or None,
                json_dumps(payload["slots"]),
                json_dumps(payload["order_snapshot"]),
                json_dumps(payload["missing_fields"]),
            ),
        )
        row = self._draft(shop_id, conversation_id) or {}
        self.hub.publish({"type": "return_record_draft", "data": row})
        return row

    def _create_record(
        self,
        *,
        shop_id: int,
        conversation_id: int,
        message_id: int,
        user_uid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing_rows: list[dict[str, Any]] = []
        order_no = self._clean_text(payload.get("order_no"))
        if user_uid and order_no:
            existing_rows = self.db.query(
                """
                SELECT id FROM return_records
                WHERE shop_id=%s AND user_uid=%s AND order_no=%s
                ORDER BY id DESC
                """,
                (shop_id, user_uid, order_no),
            )
        if existing_rows:
            record_id = int(existing_rows[0]["id"])
            self.db.execute(
                """
                UPDATE return_records
                SET conversation_id=%s,
                    message_id=%s,
                    username=%s,
                    order_status=%s,
                    record_type=%s,
                    new_address=%s,
                    remark=%s,
                    source_message=%s,
                    slots_json=%s,
                    order_snapshot_json=%s,
                    created_at=NOW()
                WHERE id=%s
                """,
                (
                    conversation_id,
                    message_id,
                    payload["username"],
                    payload["order_status"],
                    payload["record_type"],
                    payload["new_address"] or None,
                    payload["remark"] or None,
                    payload["source_message"] or None,
                    json_dumps(payload["slots"]),
                    json_dumps(payload["order_snapshot"]),
                    record_id,
                ),
            )
            duplicate_ids = [int(row["id"]) for row in existing_rows[1:]]
            if duplicate_ids:
                placeholders = ",".join(["%s"] * len(duplicate_ids))
                self.db.execute(f"DELETE FROM return_records WHERE id IN ({placeholders})", duplicate_ids)
            return self.db.query_one("SELECT * FROM return_records WHERE id=%s", (record_id,)) or {}

        record_id = self.db.execute(
            """
            INSERT INTO return_records
            (shop_id, conversation_id, message_id, user_uid, username, order_no, order_status,
             record_type, new_address, remark, source_message, slots_json, order_snapshot_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                shop_id,
                conversation_id,
                message_id,
                user_uid,
                payload["username"],
                payload["order_no"],
                payload["order_status"],
                payload["record_type"],
                payload["new_address"] or None,
                payload["remark"] or None,
                payload["source_message"] or None,
                json_dumps(payload["slots"]),
                json_dumps(payload["order_snapshot"]),
            ),
        )
        return self.db.query_one("SELECT * FROM return_records WHERE id=%s", (record_id,)) or {}

    @staticmethod
    def _sanitize_intent(intent: dict[str, Any], record_type: str, *, complete: bool) -> dict[str, Any]:
        sanitized = dict(intent)
        sanitized["intent_code"] = sanitized.get("intent_code") or "custom"
        sanitized["actions"] = [
            item
            for item in sanitized.get("actions") or []
            if not (isinstance(item, dict) and item.get("type") in RECORD_ACTIONS | {"transfer_to_human"})
        ]
        sanitized.setdefault("slots", {})
        if isinstance(sanitized["slots"], dict):
            sanitized["slots"]["return_record_type"] = record_type
            sanitized["slots"]["return_record_status"] = "completed" if complete else "draft"
        return sanitized

    @staticmethod
    def _missing_reply(record_type: str, missing_fields: list[str], order_snapshot: dict[str, Any] | None = None) -> str:
        label = RECORD_TYPE_LABELS.get(record_type, "事项")
        if "order_no" in missing_fields:
            options = ReturnRecordService._order_options_text(order_snapshot or {})
            if options:
                return f"亲，您有多个订单，我需要先确认要处理哪一个订单。请回复订单号或商品名称：{options}"
            return "亲，请您补充一下要处理的订单号或商品名称，我确认订单后再为您登记处理。"
        if "new_address" in missing_fields:
            return "亲，请您把新的完整收货地址发我一下，建议包含收件人、手机号和详细地址。"
        if "remark" in missing_fields:
            return "亲，这个情况我可以帮您登记，请您再补充一下具体需要处理的事项。"
        return "亲，当前信息还差一点，请您再补充一下相关内容。"

    @staticmethod
    def _order_options_text(snapshot: dict[str, Any]) -> str:
        orders = snapshot.get("orders")
        if not isinstance(orders, list):
            return ""
        options: list[str] = []
        for index, order in enumerate(orders[:5], start=1):
            if not isinstance(order, dict):
                continue
            order_no = ReturnRecordService._order_no_from_snapshot({"order": order})
            goods_name = ReturnRecordService._first_goods_name(order)
            label = f"{index}. "
            label += f"订单号{order_no}" if order_no else "未识别订单号"
            if goods_name:
                label += f"（{goods_name}）"
            options.append(label)
        return "；".join(options)

    @staticmethod
    def _first_goods_name(order: dict[str, Any]) -> str:
        goods_list = order.get("goods_list") or order.get("goodsList") or order.get("items") or []
        if isinstance(goods_list, list):
            for goods in goods_list:
                if not isinstance(goods, dict):
                    continue
                name = goods.get("goods_name") or goods.get("goodsName") or goods.get("name")
                if name:
                    return str(name)
        return str(order.get("goods_name") or order.get("goodsName") or order.get("goodsNameStr") or "")

    @classmethod
    def _looks_like_address_change(cls, text: str) -> bool:
        normalized = str(text or "")
        if any(keyword in normalized for keyword in ("改地址", "修改地址", "换地址", "地址改", "收货地址", "更改地址")):
            return True
        if re.search(r"(?:改|修改|更改|换).{0,4}(?:地址|收货地址|收货信息)", normalized):
            return True
        change_markers = ("改成", "改为", "修改为", "换成", "新地址", "地址是", "收货地址")
        return any(marker in normalized for marker in change_markers) and cls._looks_like_address_text(
            cls._extract_address(normalized)
        )

    @staticmethod
    def _complete_reply(record_type: str, payload: dict[str, Any] | None = None) -> str:
        label = RECORD_TYPE_LABELS.get(record_type, "事项")
        payload = payload or {}
        if record_type == "address_change" and payload.get("new_address"):
            return f"亲，收到您的地址修改需求，已为您登记，需要将订单地址改为：{payload['new_address']}。我们会尽快跟进处理，修改结果会第一时间通知您。"
        return f"亲，已为您登记{label}信息，我们会尽快跟进处理。"

    @staticmethod
    def _extract_order_no(text: str) -> str:
        text = str(text or "")
        patterns = (
            r"(?:订单号|订单编号|订单|单号)\s*(?:是|为|:|：|#)?\s*([A-Za-z0-9_-]{6,})",
            r"\b([A-Za-z0-9_-]{6,})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip("，,。.;；")
        return ""

    @staticmethod
    def _extract_address(text: str) -> str:
        text = str(text or "").strip()
        for marker in ("改成", "改为", "修改为", "换成", "新地址", "地址是", "收货地址"):
            if marker in text:
                value = text.split(marker, 1)[1].strip(" ：:，,。")
                if len(value) >= 8 and ReturnRecordService._looks_like_address_text(value):
                    return value
        return text if len(text) >= 12 and any(token in text for token in ("省", "市", "区", "县", "路", "街")) else ""

    @staticmethod
    def _looks_like_address_text(text: str) -> bool:
        text = str(text or "").strip()
        return len(text) >= 8 and any(
            token in text
            for token in ("省", "市", "区", "县", "路", "街", "巷", "号", "栋", "楼", "苑", "小区", "村", "镇", "大街", "大道")
        )

    @staticmethod
    def _extract_remark(text: str) -> str:
        text = str(text or "").strip()
        cleaned = re.sub(r"(订单号|订单编号|订单|单号)\s*[:：#]?\s*[A-Za-z0-9_-]{8,}", "", text).strip(" ，,。")
        if cleaned in {"帮我备注", "备注一下", "帮忙备注", "备注订单", "处理一下"}:
            return ""
        return cleaned if len(cleaned) >= 2 else ""

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value or "").strip()
