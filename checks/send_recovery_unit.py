from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openkefu.platforms.pdd.chat.send_policy import (
    is_session_expired,
    send_message_failure_text,
    send_message_response_ok,
)
from openkefu.web.shop_runtime import PasswordVerificationRequired, ShopRunner, ShopRuntimeManager


ACK = {"response": "send_message", "result": "ok", "msg_id": "remote-1"}


def runner_stub():
    """Use only fake DB/transport objects; never construct a live login or manager."""
    runner = ShopRunner.__new__(ShopRunner)
    runner.shop_id = 1
    runner.mall_id = "mall-1"
    runner.lock = threading.RLock()
    runner.customer_service = Mock()
    runner.customer_service.build_client_msg_id.return_value = "stable-id"
    runner.customer_service.send_text_message.return_value = dict(ACK)
    runner.token_result = {"token": "fake-token"}
    runner.session_id = None
    runner.login = Mock()
    runner.transfer_client = runner.goods_service = runner.order_service = None
    runner.listener = None
    runner.db = Mock()
    runner.db.execute.return_value = 12
    runner.db.query_one.return_value = {"id": 10, "user_uid": "user-1", "conv_id": "conv-1"}
    runner.runtime_logger = Mock()
    runner.hub = Mock()
    for name in (
        "_load_sendable_conversation", "_message_row", "_persist_current_login_cache",
        "_cancel_ws_reconnect", "_assert_shop_identity", "_set_shop_status", "_shop_row",
        "_set_error", "_start_latest_conversations_sync",
    ):
        setattr(runner, name, Mock())
    runner._is_conversation_transferred = Mock(return_value=False)
    runner._chat_type_id_for_send = Mock(return_value=9)
    runner._message_fields_for_send = Mock(return_value=None)
    runner._encrypt_secret_text = lambda text: text
    return runner


class SendPolicyTest(unittest.TestCase):
    def test_response_shapes_require_positive_acknowledgement(self):
        self.assertTrue(send_message_response_ok(ACK))
        self.assertTrue(send_message_response_ok({"success": True, "result": ACK}))
        for response in (None, {}, {**ACK, "msg_id": ""}, {**ACK, "success": False},
                         {"success": False, "result": ACK}):
            with self.subTest(response=response):
                self.assertFalse(send_message_response_ok(response))

    def test_generic_rejection_retains_business_error_code(self):
        response = {"success": False, "errorCode": 43001, "errorMsg": "request rejected"}
        self.assertIn("43001", send_message_failure_text(response))
        self.assertTrue(is_session_expired(RuntimeError(send_message_failure_text(response))))
        self.assertTrue(is_session_expired({"result": response}))

    def test_local_errors_do_not_request_login(self):
        for error in (TimeoutError("timed out"), RuntimeError("pool exhausted"),
                      RuntimeError("unrelated error 143001")):
            self.assertFalse(is_session_expired(error))


class SendRecoveryTest(unittest.TestCase):
    def test_online_returns_pending_verification_instead_of_session_error(self):
        runner = runner_stub()
        runner._require_creator_llm_quota = Mock()
        challenge = {
            "status": "verification_required",
            "need_verify": True,
            "verify_type": "mobile",
            "mask_mobile": "138****0000",
            "message": "请输入短信验证码后继续登录",
        }
        runner._connect_customer_service_from_cache = Mock(
            side_effect=PasswordVerificationRequired(challenge)
        )

        result = runner.online()

        self.assertTrue(result["need_verify"])
        self.assertEqual(result["mask_mobile"], "138****0000")
        runner._set_error.assert_not_called()

    def test_auto_relogin_keeps_login_object_for_mobile_verification(self):
        runner = runner_stub()
        runner._password_login_state = None
        runner._last_auto_relogin_at = 0.0
        runner.AUTO_RELOGIN_MIN_INTERVAL = 600.0
        runner.PASSWORD_VERIFY_TTL_SECONDS = 300
        runner._saved_credentials = Mock(return_value={"username": "13800000000", "password": "secret"})
        login = Mock()
        login.do_password_login.return_value = {
            "need_verify": True,
            "verify_type": "mobile",
            "mask_mobile": "138****0000",
        }

        with patch("openkefu.web.runtime.shop_runner.Login", return_value=login):
            self.assertFalse(runner._try_auto_relogin())

        state = runner._password_login_state
        self.assertIs(state["login"], login)
        self.assertEqual(state["username"], "13800000000")
        self.assertTrue(runner._pending_password_verification()["need_verify"])

    def test_send_sms_uses_the_login_that_created_the_challenge(self):
        verification_login = Mock()
        verification_login.send_mobile_verify_code.return_value = True
        runner = SimpleNamespace(
            _password_login_state={
                "shop_id": 1,
                "username": "13800000000",
                "expires_at": 99999999999,
                "login": verification_login,
            },
            login=Mock(),
            _set_error=Mock(),
        )
        manager = ShopRuntimeManager.__new__(ShopRuntimeManager)
        manager._sms_limiter = Mock()
        manager._sms_limiter.hit.return_value = True
        manager.runner = Mock(return_value=runner)

        self.assertTrue(manager.password_login_send_sms(1))
        verification_login.send_mobile_verify_code.assert_called_once_with("13800000000")
        runner.login.send_mobile_verify_code.assert_not_called()

    def test_cache_write_failure_does_not_repeat_acknowledged_send(self):
        runner = runner_stub()
        runner._persist_current_login_cache.side_effect = [RuntimeError("temporary DB failure"), None]
        result = runner.send_reply(conversation_id=10, content="test reply")
        self.assertEqual(result["msg_id"], "remote-1")
        self.assertEqual(runner.customer_service.send_text_message.call_count, 1)
        self.assertTrue(any("status='sent'" in c.args[0] for c in runner.db.execute.call_args_list))

    def test_rejected_text_refreshes_once_and_reuses_idempotency_key(self):
        runner = runner_stub()
        old_client = runner.customer_service
        old_client.send_text_message.return_value = {
            "success": False, "error_code": 43001, "error_msg": "request rejected",
        }
        new_client = Mock()
        new_client.send_text_message.return_value = dict(ACK)

        def reconnect(**kwargs):
            runner.customer_service = new_client
            runner.token_result = {"token": "renewed-token"}

        runner._reconnect_customer_service_from_cache = Mock(side_effect=reconnect)
        result = runner.send_reply(conversation_id=10, content="test reply")
        runner._reconnect_customer_service_from_cache.assert_called_once()
        self.assertEqual(result["_send_attempts"], 2)
        first = old_client.send_text_message.call_args.kwargs
        second = new_client.send_text_message.call_args.kwargs
        self.assertEqual(first["client_msg_id"], second["client_msg_id"])
        self.assertEqual(second["token_result"], {"token": "renewed-token"})

    def test_concurrent_rejections_share_one_reconnect(self):
        runner = runner_stub()
        failed_client = runner.customer_service
        barrier = threading.Barrier(2)

        def reconnect(**kwargs):
            runner.customer_service = Mock()

        runner._reconnect_customer_service_from_cache = Mock(side_effect=reconnect)

        def recover():
            barrier.wait(timeout=2)
            runner._recover_send_session(failed_client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(recover) for _ in range(2)]
            for future in futures:
                future.result(timeout=3)
        runner._reconnect_customer_service_from_cache.assert_called_once()

    def test_recovery_does_not_reopen_a_manually_disconnected_shop(self):
        runner = runner_stub()
        failed_client = runner.customer_service
        runner.customer_service = None
        runner._reconnect_customer_service_from_cache = Mock()
        with self.assertRaisesRegex(RuntimeError, "not online"):
            runner._recover_send_session(failed_client)
        runner._reconnect_customer_service_from_cache.assert_not_called()

    def test_failed_recovery_marks_shop_error(self):
        runner = runner_stub()
        error = RuntimeError("manual login required")
        runner._reconnect_customer_service_from_cache = Mock(side_effect=error)
        with self.assertRaisesRegex(RuntimeError, "manual login"):
            runner._recover_send_session(runner.customer_service)
        runner._set_error.assert_called_once_with(error)

    def test_rejected_image_is_not_marked_sent(self):
        runner = runner_stub()
        runner.customer_service.send_image_message.return_value = ({"success": False, "error_code": 400}, "image-url")
        with self.assertRaisesRegex(RuntimeError, "response invalid"):
            runner.send_image(conversation_id=10, image_base64="fake")
        self.assertFalse(any("status='sent'" in c.args[0] for c in runner.db.execute.call_args_list))

    def test_image_expiry_recovers_but_timeout_is_not_retried(self):
        runner = runner_stub()
        client = runner.customer_service
        client.send_image_message.side_effect = [RuntimeError("code=43001"), (dict(ACK), "image-url")]
        runner._recover_send_session = Mock()
        self.assertEqual(runner.send_image(conversation_id=10, image_base64="fake"), ACK)
        runner._recover_send_session.assert_called_once_with(client)
        client.send_image_message.reset_mock(side_effect=True)
        client.send_image_message.side_effect = TimeoutError("delivery unknown")
        with self.assertRaises(TimeoutError):
            runner.send_image(conversation_id=10, image_base64="fake")
        client.send_image_message.assert_called_once()

    def test_network_failure_does_not_trigger_password_login(self):
        runner = runner_stub()
        runner._login_from_cache = Mock(side_effect=OSError("network unavailable"))
        runner._try_auto_relogin = Mock(return_value=False)
        with self.assertRaises(OSError):
            runner._connect_customer_service_from_cache()
        runner._try_auto_relogin.assert_not_called()

    def test_legacy_timeout_is_not_retried_without_an_idempotency_key(self):
        runner = runner_stub()
        runner.db.query_one.return_value["chat_type"] = "conciliation"
        runner.customer_service.send_text_message.side_effect = TimeoutError("delivery unknown")
        with self.assertRaises(TimeoutError):
            runner.send_reply(conversation_id=10, content="test reply")
        runner.customer_service.send_text_message.assert_called_once()

    def test_service_timeout_retry_keeps_same_idempotency_key(self):
        runner = runner_stub()
        runner.customer_service.send_text_message.side_effect = [TimeoutError("delivery unknown"), dict(ACK)]
        with patch("openkefu.web.runtime.shop_runner.time.sleep"):
            runner.send_reply(conversation_id=10, content="test reply")
        calls = runner.customer_service.send_text_message.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["client_msg_id"], calls[1].kwargs["client_msg_id"])

    def test_old_send_cache_write_waits_for_credential_rotation(self):
        runner = runner_stub()
        runner.login = SimpleNamespace(generation="old")
        runner.listener = {"client": Mock()}
        cache = {"generation": "old"}
        loaded = []
        relogged, writer_started, writer_done = (threading.Event() for _ in range(3))
        runner._persist_current_login_cache = ShopRunner._persist_current_login_cache.__get__(runner)
        runner._persist_current_login_cache_locked = lambda: cache.update(generation=runner.login.generation)

        def load_login():
            loaded.append(cache["generation"])
            return SimpleNamespace(generation=cache["generation"], session=Mock())

        def relogin():
            cache["generation"] = "new"
            relogged.set()
            self.assertTrue(writer_started.wait(2))
            self.assertFalse(writer_done.wait(0.05))
            return True

        runner._login_from_cache = load_login
        runner._try_auto_relogin = relogin

        def make_client(login):
            client = Mock()
            client.get_token.return_value = (
                {"error_code": 43001} if login.generation == "old" else {"token": "new-token"}
            )
            ready = threading.Event()
            ready.set()
            client.start_titan_listener.return_value = {"client": Mock(), "ready_event": ready}
            return client

        def persist_old_send():
            self.assertTrue(relogged.wait(2))
            writer_started.set()
            runner._persist_current_login_cache()
            writer_done.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(persist_old_send)
            with patch("openkefu.web.runtime.shop_runner.CustomerServiceClient", side_effect=make_client):
                self.assertTrue(runner.hot_reconnect_customer_service())
            writer.result(timeout=2)
        self.assertEqual(loaded, ["old", "new"])
        self.assertEqual(cache["generation"], "new")

    def test_manual_offline_wins_after_an_in_progress_hot_reconnect(self):
        runner = runner_stub()
        runner.listener = {"client": Mock()}
        preparing, release, offline_started = (threading.Event() for _ in range(3))

        def load_login():
            preparing.set()
            if not release.wait(2):
                raise TimeoutError("test did not release hot reconnect")
            return Mock()

        runner._login_from_cache = load_login
        client = Mock()
        client.get_token.return_value = {"token": "new-token"}
        ready = threading.Event()
        ready.set()
        client.start_titan_listener.return_value = {"client": Mock(), "ready_event": ready}

        def offline():
            offline_started.set()
            runner.offline()

        with patch("openkefu.web.runtime.shop_runner.CustomerServiceClient", return_value=client):
            with ThreadPoolExecutor(max_workers=2) as pool:
                hot = pool.submit(runner.hot_reconnect_customer_service)
                self.assertTrue(preparing.wait(2))
                stopping = pool.submit(offline)
                self.assertTrue(offline_started.wait(2))
                self.assertFalse(stopping.done())
                release.set()
                self.assertTrue(hot.result(timeout=2))
                stopping.result(timeout=2)
        self.assertIsNone(runner.listener)
        self.assertIsNone(runner.customer_service)

    def test_post_commit_cleanup_error_keeps_new_websocket(self):
        runner = runner_stub()
        old_ws = Mock()
        old_ws.close.side_effect = OSError("close failed")
        runner.listener = {"client": old_ws}
        runner._login_from_cache = Mock(return_value=Mock())
        client = Mock()
        client.get_token.return_value = {"token": "new-token"}
        new_ws = Mock()
        ready = threading.Event()
        ready.set()
        client.start_titan_listener.return_value = {"client": new_ws, "ready_event": ready}
        with patch("openkefu.web.runtime.shop_runner.CustomerServiceClient", return_value=client):
            self.assertTrue(runner.hot_reconnect_customer_service())
        self.assertIs(runner.customer_service, client)
        self.assertIs(runner.listener["client"], new_ws)
        new_ws.close.assert_not_called()

    def test_candidate_startup_failure_does_not_mark_old_session_failed(self):
        runner = runner_stub()
        old_ws = Mock()
        runner.listener = {"client": old_ws}
        runner._login_from_cache = Mock(return_value=Mock())
        runner._on_titan_error = Mock()
        client = Mock()
        client.get_token.return_value = {"token": "new-token"}

        def start_listener(**kwargs):
            error = OSError("candidate handshake failed")
            kwargs["on_error"](error)
            return {"client": Mock(), "ready_error": error}

        client.start_titan_listener.side_effect = start_listener
        with patch("openkefu.web.runtime.shop_runner.CustomerServiceClient", return_value=client):
            self.assertFalse(runner.hot_reconnect_customer_service())
        runner._on_titan_error.assert_not_called()
        old_ws.close.assert_not_called()

    def test_hot_reconnect_uses_current_cookies_and_recovers_expiry(self):
        runner = runner_stub()
        old_ws = Mock()
        runner.listener = {"client": old_ws}
        runner._login_from_cache = Mock(side_effect=[Mock(), Mock()])
        runner._try_auto_relogin = Mock(return_value=True)
        failed, renewed = Mock(), Mock()
        failed.get_token.return_value = {"success": False, "error_code": 43001, "error_msg": "request rejected"}
        renewed.get_token.return_value = {"token": "new-token", "mall_id": "mall-1"}
        new_ws = Mock()
        ready = threading.Event()
        ready.set()
        renewed.start_titan_listener.return_value = {"client": new_ws, "ready_event": ready}
        with patch("openkefu.web.runtime.shop_runner.CustomerServiceClient", side_effect=[failed, renewed]):
            self.assertTrue(runner.hot_reconnect_customer_service())
        runner._persist_current_login_cache.assert_called_once()
        runner._try_auto_relogin.assert_called_once()
        self.assertIs(runner.listener["client"], new_ws)
        self.assertIs(runner.customer_service, renewed)
        old_ws.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
