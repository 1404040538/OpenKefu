from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from openkefu.platforms.pdd.auth.login import Login
from openkefu.platforms.pdd.chat.customer_service import CustomerServiceClient


def login_without_js() -> Login:
    login = Login.__new__(Login)
    login.session = requests.Session()
    login.cookies = {}
    login.fingerprint_env = {"navigator": {"ua": "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"}}
    return login


class CookieRefreshTest(unittest.TestCase):
    def test_pfb_refresh_survives_next_cookie_read_and_preserves_scope(self):
        login = login_without_js()
        for name in ("rckk", "_bee", "ru1k", "_f77", "ru2k", "_a42"):
            login.cookies[name] = "old"
            login.session.cookies.set(name, "old", domain=".pinduoduo.com", path="/", secure=True)

        login._apply_pfb_result({"a": ["new-a"], "b": "new-b", "c": "new-c"})

        cookies = login._known_cookies()
        for name, expected in (("rckk", "new-a"), ("_bee", "new-a"), ("ru1k", "new-b"), ("ru2k", "new-c")):
            self.assertEqual(cookies[name], expected)
        self.assertEqual(len(login.session.cookies), 6)
        for cookie in login.session.cookies:
            self.assertEqual(cookie.domain, ".pinduoduo.com")
            self.assertEqual(cookie.path, "/")
            self.assertTrue(cookie.secure)
            self.assertNotEqual(cookie.value, "old")

    def test_response_rotating_one_alias_updates_both_names(self):
        login = login_without_js()
        login.cookies = {"rckk": "old", "_bee": "old"}
        login.session.cookies.set("rckk", "old", domain=".pinduoduo.com", path="/")
        response = requests.Response()
        response.headers["Set-Cookie"] = "_bee=new; Path=/; Secure"

        login._merge_response_cookies(response)

        self.assertEqual(login._known_cookies()["_bee"], "new")
        self.assertEqual(login._known_cookies()["rckk"], "new")

    def test_chat_response_uses_cookie_merge_even_on_http_failure(self):
        login = login_without_js()
        client = CustomerServiceClient(login)
        response = requests.Response()
        response.status_code = 401
        response.headers["Set-Cookie"] = "PASS_ID=rotated; Path=/; Secure"
        with patch.object(login.session, "request", return_value=response):
            with self.assertRaises(requests.HTTPError):
                client._send("GET", "/offline-test", include_anti_content=False)

        self.assertEqual(login._known_cookies().get("PASS_ID"), "rotated")

    def test_chat_headers_match_anti_content_fingerprint(self):
        login = login_without_js()
        client = CustomerServiceClient(login)

        headers = client._headers(include_anti_content=False)

        self.assertEqual(headers["user-agent"], login._current_user_agent())
        self.assertEqual(headers["sec-ch-ua"], login._current_sec_ch_ua())

    def test_titan_token_does_not_replace_subsystem_23_cookie(self):
        login = login_without_js()
        login.home_url = "https://mms.pinduoduo.com/home/"
        login.cookies["windows_app_shop_token_23"] = "shop-token"
        login.session.cookies.set("windows_app_shop_token_23", "shop-token", domain=".pinduoduo.com", path="/")
        login.js = Mock()
        login.js.run.side_effect = [{}, {"authToken": "titan-token", "cookies": {"windows_app_shop_token_23": "titan-token"}}]
        with patch.object(login, "_request_json", return_value={}), patch.object(login, "get_latest_anti_content", return_value="anti"):
            token = CustomerServiceClient(login).get_subsystem_auth_token(20)

        self.assertEqual(token, "titan-token")
        self.assertEqual(login._known_cookies()["windows_app_shop_token_23"], "shop-token")


class ApiErrorTest(unittest.TestCase):
    def test_session_expiry_retains_machine_readable_code(self):
        response = {"success": False, "error_code": 43001, "error_msg": "expired"}

        with self.assertRaises(RuntimeError) as caught:
            CustomerServiceClient._raise_for_api_failure(response)

        self.assertEqual(getattr(caught.exception, "error_code", None), 43001)
        self.assertEqual(getattr(caught.exception, "response", None), response)

    def test_regular_success_is_not_classified_as_failure(self):
        CustomerServiceClient._raise_for_api_failure({"success": True, "errorCode": 1000000, "result": {}})


class AntiContentGenerationTest(unittest.TestCase):
    def test_existing_command_anti_does_not_spawn_unused_generation(self):
        login = login_without_js()
        login.get_latest_anti_content = Mock(return_value="fresh-outer")
        command = {"cmd": "send_message", "anti_content": "existing-inner", "message": {"content": "offline"}}

        body = CustomerServiceClient(login).build_chat_command_body(command)

        login.get_latest_anti_content.assert_called_once_with(CustomerServiceClient.CHAT_REFERER)
        self.assertEqual(body["data"], command)
        self.assertIsNot(body["data"], command)
        self.assertEqual(body["anti_content"], "fresh-outer")

    def test_missing_inner_and_explicit_anti_keep_existing_semantics(self):
        login = login_without_js()
        login.get_latest_anti_content = Mock(side_effect=["fresh-inner", "fresh-outer"])
        client = CustomerServiceClient(login)

        body = client.build_chat_command_body({"cmd": "offline"})
        self.assertEqual(body["data"]["anti_content"], "fresh-inner")
        self.assertEqual(body["anti_content"], "fresh-outer")

        login.get_latest_anti_content.reset_mock()
        body = client.build_chat_command_body({"cmd": "offline"}, anti_content="explicit")
        self.assertEqual(body["data"]["anti_content"], "explicit")
        self.assertEqual(body["anti_content"], "explicit")
        login.get_latest_anti_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
