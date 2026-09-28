import unittest

from openkefu.platforms.pdd.chat.return_records import ReturnRecordService


class FakeDb:
    def __init__(self):
        self.drafts = {}
        self.records = {}
        self.next_record_id = 1
        self.conversations = {10: {"nickname": "张三"}}

    def query_one(self, sql, params=None):
        params = tuple(params or ())
        if "FROM conversations" in sql:
            return self.conversations.get(params[0])
        if "FROM return_record_drafts" in sql:
            return self.drafts.get((params[0], params[1]))
        if "FROM return_records" in sql:
            return self.records.get(params[0])
        return None

    def query(self, sql, params=None):
        params = tuple(params or ())
        if "FROM return_records" in sql and "WHERE shop_id=%s AND user_uid=%s AND order_no=%s" in sql:
            rows = [
                {"id": record["id"]}
                for record in self.records.values()
                if record["shop_id"] == params[0] and record["user_uid"] == params[1] and record["order_no"] == params[2]
            ]
            return sorted(rows, key=lambda item: item["id"], reverse=True)
        return []

    def execute(self, sql, params=None):
        params = tuple(params or ())
        if "INSERT INTO return_record_drafts" in sql:
            key = (params[0], params[1])
            self.drafts[key] = {
                "id": 1,
                "shop_id": params[0],
                "conversation_id": params[1],
                "user_uid": params[2],
                "username": params[3],
                "record_type": params[4],
                "order_no": params[5],
                "order_status": params[6],
                "new_address": params[7],
                "remark": params[8],
                "source_message": params[9],
                "slots_json": params[10],
                "order_snapshot_json": params[11],
                "missing_fields_json": params[12],
            }
            return 1
        if "INSERT INTO return_records" in sql:
            record_id = self.next_record_id
            self.next_record_id += 1
            self.records[record_id] = {
                "id": record_id,
                "shop_id": params[0],
                "conversation_id": params[1],
                "message_id": params[2],
                "user_uid": params[3],
                "username": params[4],
                "order_no": params[5],
                "order_status": params[6],
                "record_type": params[7],
                "new_address": params[8],
                "remark": params[9],
                "source_message": params[10],
                "slots_json": params[11],
                "order_snapshot_json": params[12],
            }
            return record_id
        if "UPDATE return_records" in sql:
            record_id = params[-1]
            record = self.records[record_id]
            record.update(
                {
                    "conversation_id": params[0],
                    "message_id": params[1],
                    "username": params[2],
                    "order_status": params[3],
                    "record_type": params[4],
                    "new_address": params[5],
                    "remark": params[6],
                    "source_message": params[7],
                    "slots_json": params[8],
                    "order_snapshot_json": params[9],
                }
            )
            return 1
        if "DELETE FROM return_records WHERE id IN" in sql:
            for record_id in params:
                self.records.pop(record_id, None)
            return len(params)
        if "DELETE FROM return_record_drafts" in sql:
            self.drafts.pop((params[0], params[1]), None)
            return 1
        return 1


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class FakeLogger:
    def log(self, *args, **kwargs):
        return None


def order_lookup(order_no):
    if not order_no:
        return {
            "status": "found",
            "order_status": "待发货",
            "order_no": "AUTO123456",
            "query": {"order_no": ""},
            "order": {"order_sn": "AUTO123456", "order_status": "1"},
        }
    return {"status": "found", "order_status": "待发货", "order_no": order_no, "query": {"order_no": order_no}}


def no_order_lookup(order_no):
    return {"status": "not_found", "order_status": "未查询到订单", "query": {"order_no": order_no}}


class ReturnRecordServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.hub = FakeHub()
        self.service = ReturnRecordService(self.db, self.hub, FakeLogger())

    def handle(self, text, message_id=1):
        return self.service.handle_intent(
            shop_id=1,
            conversation_id=10,
            message_id=message_id,
            user_uid="uid-1",
            user_content=text,
            intent={
                "reply": "",
                "intent_code": "custom",
                "confidence": 1,
                "resolution_status": "need_action",
                "slots": {},
                "actions": [],
            },
            order_lookup=order_lookup,
        )

    def test_return_without_order_no_uses_local_order_lookup(self):
        result = self.handle("我要退货")
        self.assertTrue(result.handled)
        self.assertEqual(result.record["record_type"], "return")
        self.assertEqual(result.record["order_no"], "AUTO123456")
        self.assertEqual(result.intent["resolution_status"], "resolved")

    def test_address_change_completes_across_turns(self):
        first = self.handle("订单号123456789帮我改地址")
        self.assertTrue(first.handled)
        self.assertIn("完整收货地址", first.reply)

        second = self.handle("改成广东省深圳市南山区科技园1号张三13800138000", message_id=2)
        self.assertTrue(second.handled)
        self.assertEqual(second.record["record_type"], "address_change")
        self.assertEqual(second.record["order_no"], "123456789")
        self.assertIn("广东省深圳市", second.record["new_address"])
        self.assertFalse(self.db.drafts)

    def test_address_change_without_order_no_uses_local_order_lookup(self):
        first = self.handle("我的订单地址改为 车陂北门大街一巷12号起点三合苑")
        self.assertTrue(first.handled)
        self.assertEqual(first.record["record_type"], "address_change")
        self.assertEqual(first.record["order_no"], "AUTO123456")
        self.assertIn("车陂北门", first.record["new_address"])

    def test_address_change_request_then_address_never_asks_order_no(self):
        first = self.handle("我要修改下地址")
        self.assertTrue(first.handled)
        self.assertIsNotNone(first.draft)
        self.assertEqual(first.draft["order_no"], "AUTO123456")
        self.assertIn("完整收货地址", first.reply)
        self.assertNotIn("订单号", first.reply)

        second = self.handle("改成车陂北门大街一巷12号起点三合苑", message_id=2)
        self.assertTrue(second.handled)
        self.assertEqual(second.record["record_type"], "address_change")
        self.assertEqual(second.record["order_no"], "AUTO123456")
        self.assertIn("车陂北门大街", second.record["new_address"])
        self.assertIn("车陂北门大街", second.reply)
        self.assertFalse(self.db.drafts)

    def test_same_user_same_order_overwrites_existing_record(self):
        first = self.handle("我的订单地址改为 车陂北门大街一巷12号起点三合苑")
        self.assertTrue(first.handled)
        self.assertEqual(len(self.db.records), 1)
        record_id = first.record["id"]

        second = self.handle("我的订单地址改为 广东省深圳市南山区科技园1号", message_id=2)
        self.assertTrue(second.handled)
        self.assertEqual(len(self.db.records), 1)
        self.assertEqual(second.record["id"], record_id)
        self.assertIn("广东省深圳市", second.record["new_address"])
        self.assertNotIn("车陂北门", second.record["new_address"])

    def test_stale_order_no_draft_is_migrated_by_address_request(self):
        self.db.drafts[(1, 10)] = {
            "id": 1,
            "shop_id": 1,
            "conversation_id": 10,
            "user_uid": "uid-1",
            "username": "张三",
            "record_type": "address_change",
            "order_no": "",
            "order_status": "待核实",
            "new_address": None,
            "remark": None,
            "source_message": "我需要修改地址",
            "slots_json": "{}",
            "order_snapshot_json": "{}",
            "missing_fields_json": '["order_no"]',
        }

        first = self.handle("我需要修改地址")
        self.assertTrue(first.handled)
        self.assertIsNotNone(first.draft)
        self.assertEqual(first.draft["order_no"], "AUTO123456")
        self.assertIn("完整收货地址", first.reply)
        self.assertNotIn("订单号", first.reply)

        second = self.handle("车陂北门大街一巷12号起点三合苑", message_id=2)
        self.assertTrue(second.handled)
        self.assertEqual(second.record["record_type"], "address_change")
        self.assertEqual(second.record["order_no"], "AUTO123456")
        self.assertIn("车陂北门大街", second.record["new_address"])
        self.assertFalse(self.db.drafts)

    def test_batched_address_request_with_chitchat_asks_for_address_not_order_no(self):
        first = self.handle("我需要修改地址\n你好")
        self.assertTrue(first.handled)
        self.assertIsNotNone(first.draft)
        self.assertEqual(first.draft["order_no"], "AUTO123456")
        self.assertIn("完整收货地址", first.reply)
        self.assertNotIn("订单号", first.reply)

    def test_address_change_replies_fact_when_order_lookup_has_no_result(self):
        result = self.service.handle_intent(
            shop_id=1,
            conversation_id=10,
            message_id=1,
            user_uid="uid-1",
            user_content="我的订单地址改为 车陂北门大街一巷12号起点三合苑",
            intent={
                "reply": "",
                "intent_code": "custom",
                "confidence": 1,
                "resolution_status": "need_action",
                "slots": {},
                "actions": [],
            },
            order_lookup=no_order_lookup,
        )
        self.assertTrue(result.handled)
        self.assertIsNone(result.record)
        self.assertIn("没有查询到", result.reply)
        self.assertFalse(self.db.records)

    def test_chitchat_does_not_repeat_draft_prompt(self):
        first = self.service.handle_intent(
            shop_id=1,
            conversation_id=10,
            message_id=1,
            user_uid="uid-1",
            user_content="改地址",
            intent={
                "reply": "",
                "intent_code": "custom",
                "confidence": 1,
                "resolution_status": "need_action",
                "slots": {},
                "actions": [],
            },
            order_lookup=order_lookup,
        )
        self.assertTrue(first.handled)

        second = self.handle("还在吗", message_id=2)
        self.assertIsNone(second)

    def test_other_requires_remark(self):
        result = self.handle("订单号123456789012帮我备注")
        self.assertTrue(result.handled)
        self.assertEqual(result.draft["record_type"], "other")
        self.assertIn("具体需要处理", result.reply)

    def test_plain_shipping_consultation_does_not_create_logistics_intercept(self):
        result = self.service.handle_intent(
            shop_id=1,
            conversation_id=10,
            message_id=1,
            user_uid="uid-1",
            user_content="发什么快递",
            intent={
                "reply": "亲，会根据仓库和收货地址匹配快递，具体以发出后的物流信息为准。",
                "intent_code": "express_exception",
                "confidence": 0.9,
                "resolution_status": "resolved",
                "slots": {},
                "actions": [{"type": "express_exception_followup", "payload": {}}],
            },
            order_lookup=order_lookup,
        )
        self.assertIsNone(result)
        self.assertFalse(self.db.records)
        self.assertFalse(self.db.drafts)

    def test_plain_delivery_time_question_does_not_create_other_record(self):
        result = self.service.handle_intent(
            shop_id=1,
            conversation_id=10,
            message_id=1,
            user_uid="uid-1",
            user_content="什么时候发货？",
            intent={
                "reply": "亲，发货时间需要以订单页面或仓库处理进度为准。",
                "intent_code": "order_remark",
                "confidence": 0.85,
                "resolution_status": "resolved",
                "slots": {},
                "actions": [{"type": "order_remark", "payload": {}}],
            },
            order_lookup=order_lookup,
        )
        self.assertIsNone(result)
        self.assertFalse(self.db.records)
        self.assertFalse(self.db.drafts)


if __name__ == "__main__":
    unittest.main()
