import json
import unittest

from openkefu.platforms.pdd.chat.intent import build_intent_messages
from openkefu.platforms.pdd.chat.llm_reply import analyze_customer_intent


class IntentPromptTest(unittest.TestCase):
    def test_empty_knowledge_is_internal_and_does_not_request_customer_facing_kb_reply(self):
        messages = build_intent_messages(
            [{"role": "user", "content": "发什么快递"}],
            knowledge_hits=[],
        )

        prompt_text = "\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("不要在回复中提到知识库或未配置知识库", prompt_text)
        self.assertIn("发什么快递", prompt_text)
        self.assertNotIn("未命中时如实告知未查到", prompt_text)

    def test_empty_knowledge_shipping_consultation_repairs_bad_llm_reply(self):
        class FakeClient:
            model = "fake"

            def chat(self, *args, **kwargs):
                return (
                    '{"reply":"您好您还没有配置消费者可到的常见问题回答，建议您尽快完善配置。",'
                    '"intent_code":"express_exception","confidence":0.9,"resolution_status":"need_action",'
                    '"slots":{},"actions":[{"type":"express_exception_followup","payload":{}}]}'
                )

        intent = analyze_customer_intent(
            [{"role": "user", "content": "发什么快递"}],
            knowledge_hits=[],
            llm_client=FakeClient(),
        )

        self.assertEqual(intent["resolution_status"], "resolved")
        self.assertEqual(intent["intent_code"], "custom")
        self.assertNotIn("知识库", intent["reply"])
        self.assertNotIn("常见问题", intent["reply"])
        self.assertFalse(intent["actions"])
        self.assertTrue(intent["raw"]["empty_knowledge_reply_repaired"])

    def test_reply_containing_human_words_does_not_force_transfer(self):
        class FakeClient:
            model = "fake"

            def chat(self, *args, **kwargs):
                return json.dumps(
                    {
                        "reply": "亲，您的问题我已记录，如需人工客服可以随时告诉我哦。",
                        "intent_code": "product_consult",
                        "confidence": 0.9,
                        "resolution_status": "resolved",
                        "slots": {},
                        "actions": [],
                    }
                )

        intent = analyze_customer_intent(
            [{"role": "user", "content": "这个手机壳有黑色的吗"}],
            llm_client=FakeClient(),
        )
        self.assertFalse(
            any(item["type"] == "transfer_to_human" for item in intent["actions"]),
            "客服回复中的'人工客服'话术不应触发转人工",
        )

    def test_customer_requesting_human_still_triggers_transfer(self):
        class FakeClient:
            model = "fake"

            def chat(self, *args, **kwargs):
                return json.dumps(
                    {
                        "reply": "好的，马上为您转接人工客服。",
                        "intent_code": "custom",
                        "confidence": 0.9,
                        "resolution_status": "need_human",
                        "slots": {},
                        "actions": [],
                    }
                )

        intent = analyze_customer_intent(
            [{"role": "user", "content": "我要找人工客服处理"}],
            llm_client=FakeClient(),
        )
        self.assertTrue(any(item["type"] == "transfer_to_human" for item in intent["actions"]))

    def test_json_fenced_output_is_parsed(self):
        class FakeClient:
            model = "fake"

            def chat(self, *args, **kwargs):
                return (
                    "```json\n"
                    '{"reply":"亲，这款有现货哦。","intent_code":"product_consult",'
                    '"confidence":0.9,"resolution_status":"resolved","slots":{},"actions":[]}'
                    "\n```"
                )

        intent = analyze_customer_intent(
            [{"role": "user", "content": "这个手机壳有黑色的吗"}],
            llm_client=FakeClient(),
        )
        self.assertEqual(intent["intent_code"], "product_consult")
        self.assertEqual(intent["reply"], "亲，这款有现货哦。")


if __name__ == "__main__":
    unittest.main()
