from __future__ import annotations

import unittest

from openkefu.web.realtime import RealtimeClient, RealtimeHub


class RealtimeHubAuthorizationTest(unittest.TestCase):
    def test_user_receives_assigned_shop_event(self):
        client = RealtimeClient(user_id=10, role="user", shop_ids=frozenset({1, 2}))

        self.assertTrue(RealtimeHub._can_receive(client, {"type": "message", "data": {"shop_id": 2}}))

    def test_user_does_not_receive_unassigned_shop_event(self):
        client = RealtimeClient(user_id=10, role="user", shop_ids=frozenset({1, 2}))

        self.assertFalse(RealtimeHub._can_receive(client, {"type": "message", "data": {"shop_id": 3}}))

    def test_user_does_not_receive_event_without_authorization_dimension(self):
        client = RealtimeClient(user_id=10, role="user", shop_ids=frozenset({1, 2}))

        self.assertFalse(RealtimeHub._can_receive(client, {"type": "server_status", "data": {"cpu_percent": 30}}))

    def test_user_receives_own_user_event(self):
        client = RealtimeClient(user_id=10, role="user", shop_ids=frozenset({1, 2}))

        self.assertTrue(RealtimeHub._can_receive(client, {"type": "llm_quota_exceeded", "data": {"user_id": 10}}))
        self.assertFalse(RealtimeHub._can_receive(client, {"type": "llm_quota_exceeded", "data": {"user_id": 11}}))

    def test_admin_receives_global_events(self):
        client = RealtimeClient(user_id=1, role="admin", shop_ids=frozenset())

        self.assertTrue(RealtimeHub._can_receive(client, {"type": "server_status", "data": {"cpu_percent": 30}}))
        self.assertTrue(RealtimeHub._can_receive(client, {"type": "message", "data": {"shop_id": 99}}))


if __name__ == "__main__":
    unittest.main()
