from __future__ import annotations

import os
import re
import unittest
from dataclasses import replace
from unittest.mock import patch

import pymysql
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openkefu.web import app as app_module
from openkefu.web.config import load_config


TEST_DATABASE = "openkefu_e2e_test"
TEST_SECRETS = {
    "OPENKEFU_JWT_SECRET": "e2e-Jwt-1a2B3c4D5e6F7g8H9i0J1k2L3m4N5",
    "OPENKEFU_RUNTIME_COMMAND_SECRET": "e2e-Cmd-9z8Y7x6W5v4U3t2S1r0Q9p8O7n6M5",
    "OPENKEFU_DATA_ENCRYPTION_KEY": "e2e-Data-2m3N4b5V6c7X8z9A0s1D2f3G4h5J6",
    "OPENKEFU_RUNTIME_ROLE": "both",
    "OPENKEFU_RATE_LIMIT_DISABLED": "1",
    "OPENKEFU_SETUP_ADMIN_IP_WHITELIST": "testclient",
}


class LocalApiE2ETest(unittest.TestCase):
    """Local-only API/WS coverage using an isolated MySQL database.

    The suite never starts a PDD (Pinduoduo) login/runtime or calls an LLM/embedding endpoint.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not re.fullmatch(r"openkefu_[a-z0-9_]*test", TEST_DATABASE):
            raise RuntimeError(f"unsafe test database name: {TEST_DATABASE}")

        cls.env_patch = patch.dict(os.environ, TEST_SECRETS, clear=False)
        cls.env_patch.start()
        base_config = load_config()
        cls.config = replace(
            base_config,
            mysql=replace(base_config.mysql, database=TEST_DATABASE),
            runtime=replace(base_config.runtime, role="both", worker_id="e2e-local-worker"),
            logging=replace(base_config.logging, path="logs/e2e_local_test.log"),
        )
        cls._drop_test_database()

        cls.config_patch = patch.object(app_module, "load_config", return_value=cls.config)
        cls.config_patch.start()
        cls.app = app_module.create_app()
        cls.client_context = TestClient(cls.app)
        cls.client = cls.client_context.__enter__()

        response = cls.client.post(
            "/api/auth/setup-admin",
            json={"username": "e2e_admin", "password": "Admin123!E2E!", "display_name": "E2E Admin"},
        )
        cls._assert_response(response, 200)
        cls.admin_token = response.json()["token"]
        cls.admin_headers = {"Authorization": f"Bearer {cls.admin_token}"}

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if hasattr(cls, "client_context"):
                cls.client_context.__exit__(None, None, None)
        finally:
            if hasattr(cls, "config_patch"):
                cls.config_patch.stop()
            try:
                if hasattr(cls, "config"):
                    cls._drop_test_database()
            finally:
                if hasattr(cls, "env_patch"):
                    cls.env_patch.stop()

    @classmethod
    def _drop_test_database(cls) -> None:
        if not re.fullmatch(r"openkefu_[a-z0-9_]*test", TEST_DATABASE):
            raise RuntimeError(f"refusing to drop unsafe database name: {TEST_DATABASE}")
        mysql = cls.config.mysql
        connection = pymysql.connect(
            host=mysql.host,
            port=mysql.port,
            user=mysql.user,
            password=mysql.password,
            charset=mysql.charset,
            connect_timeout=5,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{TEST_DATABASE}`")
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _assert_response(response, expected: int) -> None:
        if response.status_code != expected:
            raise AssertionError(
                f"expected HTTP {expected}, got {response.status_code}: {response.text}"
            )

    def test_management_api_and_realtime_flow(self) -> None:
        response = self.client.get("/api/auth/status")
        self._assert_response(response, 200)
        self.assertTrue(response.json()["initialized"])

        response = self.client.post(
            "/api/auth/login",
            json={"username": "e2e_admin", "password": "wrong-password"},
        )
        self._assert_response(response, 401)
        response = self.client.get("/api/me", headers=self.admin_headers)
        self._assert_response(response, 200)
        self.assertEqual(response.json()["role"], "admin")

        response = self.client.post(
            "/api/shops",
            headers=self.admin_headers,
            json={"name": "E2E Shop", "remark": "created by automated test"},
        )
        self._assert_response(response, 200)
        shop_id = int(response.json()["id"])

        conversation_id = self.app.state.db.execute(
            """
            INSERT INTO conversations
            (shop_id, mall_id, conv_id, user_uid, nickname, last_message_preview, unread_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (shop_id, "e2e-mall", "e2e-conversation", "e2e-customer", "E2E Customer", "hello", 1),
        )
        message_id = self.app.state.db.execute(
            """
            INSERT INTO messages
            (shop_id, conversation_id, direction, msg_id, user_uid, sender_role, kind, content, status)
            VALUES (%s,%s,'inbound',%s,%s,'user','text',%s,'received')
            """,
            (shop_id, conversation_id, "e2e-message-1", "e2e-customer", "realtime hello"),
        )

        with self.client.websocket_connect(
            "/ws", subprotocols=[f"openkefu-auth.{self.admin_token}"]
        ) as websocket:
            response = self.client.patch(
                f"/api/shops/{shop_id}",
                headers=self.admin_headers,
                json={"remark": "realtime-updated"},
            )
            self._assert_response(response, 200)
            event = websocket.receive_json()
            self.assertEqual(event["type"], "shop_status")
            self.assertEqual(int(event["data"]["id"]), shop_id)
            self.assertEqual(event["data"]["remark"], "realtime-updated")

            message_row = self.app.state.db.query_one("SELECT * FROM messages WHERE id=%s", (message_id,))
            self.app.state.hub.publish({"type": "message", "data": message_row})
            event = websocket.receive_json()
            self.assertEqual(event["type"], "message")
            self.assertEqual(int(event["data"]["shop_id"]), shop_id)
            self.assertEqual(int(event["data"]["conversation_id"]), conversation_id)
            self.assertEqual(event["data"]["content"], "realtime hello")

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_text()

        response = self.client.post(
            "/api/users",
            headers=self.admin_headers,
            json={
                "username": "e2e_service",
                "password": "Service123!E2E!",
                "display_name": "E2E Service",
                "role": "service",
                "shop_ids": [shop_id],
            },
        )
        self._assert_response(response, 200)
        service_user_id = int(response.json()["id"])
        response = self.client.post(
            "/api/auth/login",
            json={"username": "e2e_service", "password": "Service123!E2E!"},
        )
        self._assert_response(response, 200)
        service_headers = {"Authorization": f"Bearer {response.json()['token']}"}
        response = self.client.get("/api/shops", headers=service_headers)
        self._assert_response(response, 200)
        self.assertEqual([int(item["id"]) for item in response.json()], [shop_id])
        self._assert_response(self.client.get("/api/users", headers=service_headers), 403)
        self._assert_response(self.client.get("/api/logs", headers=service_headers), 403)

        response = self.client.post(
            "/api/knowledge-bases",
            headers=self.admin_headers,
            json={"name": "E2E KB", "description": "contract test", "shop_ids": [shop_id]},
        )
        self._assert_response(response, 200)
        kb_id = int(response.json()["id"])
        response = self.client.patch(
            f"/api/knowledge-bases/{kb_id}/qa-items",
            headers=self.admin_headers,
            json={"items": [{"question": "什么时候发货？", "reply": "通常48小时内发货。"}]},
        )
        self._assert_response(response, 200)
        with patch.object(
            self.app.state.knowledge,
            "upload_file_bytes",
            return_value={"id": 1, "filename": "guide.txt", "status": "ready"},
        ):
            response = self.client.post(
                f"/api/knowledge-bases/{kb_id}/files",
                headers=self.admin_headers,
                files={"file": ("guide.txt", b"safe local test", "text/plain")},
            )
            self._assert_response(response, 200)
        response = self.client.get(f"/api/knowledge-bases/{kb_id}", headers=self.admin_headers)
        self._assert_response(response, 200)
        self.assertEqual(len(response.json()["qa_items"]), 1)

        response = self.client.post(
            "/api/note-sets",
            headers=self.admin_headers,
            json={"name": "E2E Notes", "description": "constraints", "shop_ids": [shop_id]},
        )
        self._assert_response(response, 200)
        note_set_id = int(response.json()["id"])
        response = self.client.post(
            f"/api/note-sets/{note_set_id}/items",
            headers=self.admin_headers,
            json={"content": "不要承诺具体快递公司"},
        )
        self._assert_response(response, 200)
        note_item_id = int(response.json()["id"])
        response = self.client.patch(
            f"/api/note-sets/{note_set_id}/items/{note_item_id}",
            headers=self.admin_headers,
            json={"content": "不要承诺具体快递公司和准确到货时间"},
        )
        self._assert_response(response, 200)

        response = self.client.post(
            "/api/return-records",
            headers=self.admin_headers,
            json={
                "shop_id": shop_id,
                "user_uid": "e2e-user-1",
                "username": "E2E Customer",
                "order_no": "E2E-ORDER-001",
                "record_type": "address_change",
                "new_address": "测试地址，不会用于真实订单",
            },
        )
        self._assert_response(response, 200)
        return_record_id = int(response.json()["id"])
        response = self.client.get("/api/return-records", headers=self.admin_headers)
        self._assert_response(response, 200)
        self.assertEqual(response.json()["total"], 1)
        response = self.client.get("/api/return-records/export", headers=self.admin_headers)
        self._assert_response(response, 200)
        self.assertTrue(response.content.startswith(b"PK"))

        self._assert_response(self.client.get("/api/logs", headers=self.admin_headers), 200)
        self._assert_response(self.client.get("/api/runtime/workers", headers=self.admin_headers), 200)
        # 服务器状态监控已移除：端点应返回 404
        self._assert_response(self.client.get("/api/server-status/latest", headers=self.admin_headers), 404)
        self._assert_response(
            self.client.get("/api/server-status/history?range=1h", headers=self.admin_headers), 404
        )

        self._assert_response(
            self.client.delete(f"/api/return-records/{return_record_id}", headers=self.admin_headers), 200
        )
        self._assert_response(
            self.client.delete(
                f"/api/note-sets/{note_set_id}/items/{note_item_id}", headers=self.admin_headers
            ),
            200,
        )
        self._assert_response(
            self.client.delete(f"/api/note-sets/{note_set_id}", headers=self.admin_headers), 200
        )
        self._assert_response(
            self.client.delete(f"/api/knowledge-bases/{kb_id}", headers=self.admin_headers), 200
        )

        response = self.client.patch(
            f"/api/users/{service_user_id}",
            headers=self.admin_headers,
            json={"is_active": False},
        )
        self._assert_response(response, 200)
        self._assert_response(self.client.get("/api/me", headers=service_headers), 401)
        self._assert_response(
            self.client.delete(f"/api/shops/{shop_id}", headers=self.admin_headers), 200
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
