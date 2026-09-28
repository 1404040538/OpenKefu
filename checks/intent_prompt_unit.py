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


if __name__ == "__main__":
    unittest.main()
