import json
import hashlib
import logging
import os
import random
import re
import requests
import socket
import ssl
import struct
import subprocess
import threading
import time
import zlib
from urllib.parse import parse_qs, urlparse

from openkefu.platforms.pdd.chat.titan_codec import (
    TITAN_UPSTREAM_SCHEMA,
    decode_message,
    decode_titan_frame,
    summarize_downstream,
)
from openkefu.platforms.pdd.chat.titan_ws_client import TitanClientConfig, TitanWebSocketClient
from openkefu.platforms.pdd.common import json_or_text, json_request_data
from openkefu.platforms.pdd.config import (
    BASE_URL as CONFIG_BASE_URL,
    DEFAULT_USER_AGENT,
    SEC_WEBSOCKET_KEY_JS,
    SPIDER_FONT_SOURCE,
    TITAN_SUB_SYSTEM_ID as CONFIG_TITAN_SUB_SYSTEM_ID,
    WS_BASE_URL as CONFIG_WS_BASE_URL,
    WS_USER_AGENT as CONFIG_WS_USER_AGENT,
)


logger = logging.getLogger(__name__)


class PddApiError(RuntimeError):
    """A rejected API request with its original business error code."""

    def __init__(self, message, *, error_code=None, response=None):
        self.error_code = error_code
        self.response = response
        super().__init__(f"{message}: code={error_code}, response={response!r}")


class CustomerServiceClient:
    BASE_URL = CONFIG_BASE_URL
    CHAT_REFERER = f"{CONFIG_BASE_URL}/chat-merchant/index.html"
    CHAT_VERSION_RE = re.compile(r"chat-merchant-v(?P<version>\d{8}\.\d{2}\.\d{2}\.\d{2})")
    SERVICE_BUILD_TIME_RE = re.compile(r"\bSERVICE_BUILD_TIME\b\s*[:=]\s*[\"']?(?P<version>\d{12})[\"']?")
    ASSET_VERSION_RE = re.compile(r"chat-merchant[^\"'<>]*[._-]v(?P<version>\d{14})(?:[_./-]|$)")
    DEFAULT_SERVICE_BUILD_TIME = "202604301140"
    SEC_WS_JS = SEC_WEBSOCKET_KEY_JS
    WS_BASE_URL = CONFIG_WS_BASE_URL
    TITAN_SUB_SYSTEM_ID = CONFIG_TITAN_SUB_SYSTEM_ID
    WS_USER_AGENT = CONFIG_WS_USER_AGENT
    SERVICE_CHAT_TYPE_ID = 9
    LATEST_CONVERSATIONS_PATH = "/plateau/chat/latest_conversations"
    SEND_MESSAGE_PATH = "/plateau/chat/send_message"
    PRE_UPLOAD_PATH = "/plateau/file/pre_upload"
    SEND_RANDOM_SECRET = "rpZigw#&iy$!KQD8"
    PFB_ACTIVE_INTERVAL_SECONDS = (50.0, 80.0)
    PFB_IDLE_INTERVAL_SECONDS = (540.0, 660.0)
    _HASH_IV_1 = tuple(
        value & 0xFFFFFFFF
        for value in (
            -1658786019,
            1493325398,
            302215178,
            135624897,
            -783129214,
            -2103672236,
            -1658334583,
            -1121316849,
        )
    )
    _HASH_IV_2 = tuple(
        value & 0xFFFFFFFF
        for value in (
            1755450666,
            714729647,
            1577142459,
            1932142337,
            404359373,
            -1842008283,
            694480344,
            -1009175083,
        )
    )
    _HASH_K = (
        1116352408,
        1899447441,
        3049323471,
        3921009573,
        961987163,
        1508970993,
        2453635748,
        2870763221,
        3624381080,
        310598401,
        607225278,
        1426881987,
        1925078388,
        2162078206,
        2614888103,
        3248222580,
        3835390401,
        4022224774,
        264347078,
        604807628,
        770255983,
        1249150122,
        1555081692,
        1996064986,
        2554220882,
        2821834349,
        2952996808,
        3210313671,
        3336571891,
        3584528711,
        113926993,
        338241895,
        666307205,
        773529912,
        1294757372,
        1396182291,
        1695183700,
        1986661051,
        2177026350,
        2456956037,
        2730485921,
        2820302411,
        3259730800,
        3345764771,
        3516065817,
        3600352804,
        4094571909,
        275423344,
        430227734,
        506948616,
        659060556,
        883997877,
        958139571,
        1322822218,
        1537002063,
        1747873779,
        1955562222,
        2024104815,
        2227730452,
        2361852424,
        2428436474,
        2756734187,
        3204031479,
        3329325298,
    )
    _request_counter = 1
    _request_counter_lock = threading.Lock()

    def __init__(self, login):
        self.login = login
        self.session = login.session
        self._http_lock = threading.RLock()

    def _cookies(self):
        return self.login._known_cookies()

    def _headers(self, referer=None, headers=None, include_anti_content=True):
        referer = referer or self.CHAT_REFERER
        request_headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "origin": self.BASE_URL,
            "priority": "u=1, i",
            "referer": referer,
            "sec-ch-ua": self.login._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.login._current_user_agent(),
        }
        if include_anti_content:
            request_headers["anti-content"] = self.login.get_latest_anti_content(referer)
        request_headers.update(headers or {})
        return request_headers

    def _send(self, method, path, *, referer=None, timeout=15, headers=None, include_anti_content=True, base_url_override=False, **kwargs):
        with self._http_lock:
            url = path if base_url_override else f"{self.BASE_URL}{path}"
            response = self.session.request(
                method,
                url,
                cookies=self._cookies(),
                headers=self._headers(
                    referer=referer,
                    headers=headers,
                    include_anti_content=include_anti_content,
                ),
                timeout=timeout,
                **kwargs,
            )
            self.login._merge_response_cookies(response)
            response.raise_for_status()
            return response

    def _request(self, method, path, *, referer=None, timeout=15, headers=None, **kwargs):
        response = self._send(method, path, referer=referer, timeout=timeout, headers=headers, **kwargs)
        return json_or_text(response)

    def _request_text(self, method, path, *, referer=None, timeout=15, headers=None, include_anti_content=True, **kwargs):
        response = self._send(
            method,
            path,
            referer=referer,
            timeout=timeout,
            headers=headers,
            include_anti_content=include_anti_content,
            **kwargs,
        )
        return response.text

    def get_token(self, version=3):
        return self._request(
            "POST",
            "/chats/getToken",
            files={"version": (None, str(version))},
        )

    def get_subsystem_auth_token(self, sub_system_id):
        token = self.login.ensure_windows_app_shop_token_23(sub_system_id)
        if not token:
            raise RuntimeError(f"subsystem auth token is empty: subSystemId={sub_system_id}")
        return token

    def get_titan_auth_token(self):
        return self.get_subsystem_auth_token(self.TITAN_SUB_SYSTEM_ID)

    def get_latest_conversations(self, *, offset=0, size=50, client=1, referer=None, timeout=15):
        referer = referer or self.CHAT_REFERER
        anti_content = self.login.get_latest_anti_content(referer)
        body = {
            "data": {
                "offset": int(offset or 0),
                "size": int(size or 50),
            },
            "client": client,
            "anti_content": anti_content,
        }
        response = self._send(
            "POST",
            self.LATEST_CONVERSATIONS_PATH,
            referer=referer,
            timeout=timeout,
            include_anti_content=True,
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            data=json_request_data(body),
        )
        result = json_or_text(response)
        logger.debug(
            "latest conversations fetched offset=%s size=%s client=%s result_type=%s",
            int(offset or 0),
            int(size or 50),
            client,
            type(result).__name__,
        )
        self._raise_for_api_failure(result)
        return self._unwrap_result(result)

    @staticmethod
    def _unwrap_result(data):
        if isinstance(data, dict) and data.get("success") is True and "result" in data:
            return data["result"]
        return data

    def get_realtime_user_info(self):
        return self._unwrap_result(
            self._request("GET", "/chats/userinfo/realtime?get_response=true")
        )

    def get_realtime_uid(self, token_result=None):
        user_info = self.get_realtime_user_info()
        uid = str(
            user_info.get("id")
            or user_info.get("user_id")
            or (token_result or {}).get("uid")
            or ""
        )
        if not uid:
            raise RuntimeError(f"cannot find uid from user_info={user_info!r}")
        return uid

    @classmethod
    def _next_request_id(cls):
        with cls._request_counter_lock:
            if cls._request_counter > 1_000_000:
                cls._request_counter = 1
            cls._request_counter += 1
            return int(time.time() * 1000) + cls._request_counter

    @staticmethod
    def _pass_id_identity(cookies):
        match = re.search(r"_(?P<mall_id>\d+)_(?P<uid>\d+)$", (cookies or {}).get("PASS_ID", ""))
        return match.groupdict() if match else {}

    @staticmethod
    def _first_value(data, keys):
        for key in keys:
            value = (data or {}).get(key)
            if value not in (None, ""):
                return value
        return None

    def resolve_chat_identity(self, token_result=None, *, mall_id=None, global_uid=None):
        token_result = dict(token_result or {})
        pass_id_identity = self._pass_id_identity(self._cookies())

        mall_id = mall_id or self._first_value(
            token_result,
            ("mall_id", "mallId", "mallID"),
        ) or pass_id_identity.get("mall_id")
        global_uid = global_uid or self._first_value(
            token_result,
            ("uid", "user_id", "userId", "global_uid", "globalUid"),
        ) or pass_id_identity.get("uid")

        if not mall_id:
            fresh_token_result = self.get_token()
            token_result.update(fresh_token_result if isinstance(fresh_token_result, dict) else {})
            mall_id = self._first_value(token_result, ("mall_id", "mallId", "mallID"))
            global_uid = global_uid or self._first_value(
                token_result,
                ("uid", "user_id", "userId", "global_uid", "globalUid"),
            )

        if not global_uid:
            try:
                global_uid = self.get_realtime_uid(token_result)
            except Exception:
                global_uid = ""

        if not mall_id:
            raise RuntimeError(f"cannot find mall_id from token_result={token_result!r} or PASS_ID cookie")
        return str(mall_id), str(global_uid or "")

    @classmethod
    def build_message_hash(cls, user_uid, mall_id, request_id):
        source = f"uid={user_uid}&mall_id={mall_id}&request_id={request_id}"
        first = cls._custom_sha256(source.encode("utf-8"), cls._HASH_IV_1)
        return cls._custom_sha256(first, cls._HASH_IV_2).hex()

    @classmethod
    def _custom_sha256(cls, payload, initial_state):
        mask = 0xFFFFFFFF
        data = bytearray(payload)
        data.append(0x80)
        block_count = (len(data) + 8 + 63) // 64
        words = [0] * (block_count * 16)

        for index, byte in enumerate(data):
            word_index = index // 4
            shift = 24 - 8 * (index % 4)
            words[word_index] = cls._js_or(words[word_index], cls._js_lshift(byte, shift))

        bit_length_source = len(data) + 4 * 16 - 1
        words[-2] = (8 * bit_length_source // 2**32) & mask
        words[-1] = cls._js_and(8 * bit_length_source, mask)

        state = [cls._int32(value) for value in initial_state]
        for block_start in range(0, len(words), 16):
            schedule = words[block_start:block_start + 16] + [0] * 48
            for index in range(16, 64):
                schedule[index] = cls._js_and(
                    cls._small_sigma1(schedule[index - 2])
                    + schedule[index - 7]
                    + cls._small_sigma0(schedule[index - 15])
                    + schedule[index - 16],
                    mask,
                )

            a, b, c, d, e, f, g, h = state
            for index in range(64):
                temp1 = h + cls._big_sigma1(e) + cls._choice(e, f, g) + cls._HASH_K[index] + schedule[index]
                temp2 = cls._big_sigma0(a) + cls._majority(a, b, c)
                h = g
                g = f
                f = e
                e = cls._js_and(d + temp1, mask)
                d = c
                c = b
                b = a
                a = cls._js_and(temp1 + temp2, mask)

            state = [
                cls._js_and(state[0] + a, mask),
                cls._js_and(state[1] + b, mask),
                cls._js_and(state[2] + c, mask),
                cls._js_and(state[3] + d, mask),
                cls._js_and(state[4] + e, mask),
                cls._js_and(state[5] + f, mask),
                cls._js_and(state[6] + g, mask),
                cls._js_and(state[7] + h, mask),
            ]

        return b"".join(
            bytes(
                (
                    cls._js_urshift(value, 24),
                    cls._js_urshift(value, 16) & 0xFF,
                    cls._js_urshift(value, 8) & 0xFF,
                    cls._uint32(value) & 0xFF,
                )
            )
            for value in state
        )

    @staticmethod
    def _uint32(value):
        return int(value) & 0xFFFFFFFF

    @classmethod
    def _int32(cls, value):
        value = cls._uint32(value)
        return value - 0x100000000 if value >= 0x80000000 else value

    @classmethod
    def _js_and(cls, left, right):
        return cls._int32(cls._uint32(left) & cls._uint32(right))

    @classmethod
    def _js_or(cls, left, right):
        return cls._int32(cls._uint32(left) | cls._uint32(right))

    @classmethod
    def _js_xor(cls, left, right):
        return cls._int32(cls._uint32(left) ^ cls._uint32(right))

    @classmethod
    def _js_not(cls, value):
        return cls._int32(~cls._uint32(value))

    @classmethod
    def _js_lshift(cls, value, bits):
        return cls._int32(cls._uint32(value) << bits)

    @classmethod
    def _js_urshift(cls, value, bits):
        return cls._uint32(value) >> bits

    @classmethod
    def _rotate_right(cls, value, bits):
        return cls._js_or(cls._js_urshift(value, bits), cls._js_lshift(value, 32 - bits))

    @classmethod
    def _big_sigma0(cls, value):
        return cls._js_xor(
            cls._js_xor(cls._rotate_right(value, 2), cls._rotate_right(value, 13)),
            cls._rotate_right(value, 22),
        )

    @classmethod
    def _big_sigma1(cls, value):
        return cls._js_xor(
            cls._js_xor(cls._rotate_right(value, 6), cls._rotate_right(value, 11)),
            cls._rotate_right(value, 25),
        )

    @classmethod
    def _small_sigma0(cls, value):
        return cls._js_xor(
            cls._js_xor(cls._rotate_right(value, 7), cls._rotate_right(value, 18)),
            cls._js_urshift(value, 3),
        )

    @classmethod
    def _small_sigma1(cls, value):
        return cls._js_xor(
            cls._js_xor(cls._rotate_right(value, 17), cls._rotate_right(value, 19)),
            cls._js_urshift(value, 10),
        )

    @classmethod
    def _choice(cls, value, first, second):
        return cls._js_xor(cls._js_and(value, first), cls._js_and(cls._js_not(value), second))

    @classmethod
    def _majority(cls, value, first, second):
        return cls._js_xor(
            cls._js_xor(cls._js_and(value, first), cls._js_and(value, second)),
            cls._js_and(first, second),
        )

    @classmethod
    def build_send_random(cls, *, mall_id, global_uid, user_uid, content):
        stable = "@".join(
            [
                str(mall_id),
                str(global_uid or ""),
                str(user_uid),
                str(content),
                cls.SEND_RANDOM_SECRET,
            ]
        )
        # The upstream chat protocol requires this MD5-shaped message hash;
        # it is an identifier, not a security primitive.
        prefix = hashlib.md5(stable.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        suffix_seed = os.urandom(16).hex()
        suffix = hashlib.md5(suffix_seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        return prefix + suffix

    def build_text_message_command(
        self,
        user_uid,
        content,
        *,
        token_result=None,
        mall_id=None,
        global_uid=None,
        request_id=None,
        timestamp=None,
        anti_content=None,
        chat_type=None,
        msg_type=0,
        message_fields=None,
        command_fields=None,
        force_send_sensitive=None,
        referer=None,
    ):
        if user_uid in (None, ""):
            raise ValueError("user_uid is required")

        mall_id, global_uid = self.resolve_chat_identity(
            token_result,
            mall_id=mall_id,
            global_uid=global_uid,
        )
        content = "" if content is None else str(content)
        user_uid = str(user_uid)
        request_id = int(request_id or self._next_request_id())
        timestamp = int(timestamp or time.time())
        anti_content = anti_content or self.login.get_latest_anti_content(referer or self.CHAT_REFERER)

        message = {
            "to": {
                "role": "user",
                "uid": user_uid,
            },
            "from": {
                "role": "mall_cs",
            },
            "ts": timestamp,
            "content": content,
            "msg_id": None,
            "type": msg_type or 0,
            "is_aut": 0,
            "manual_reply": 1,
            "status": "read",
            "is_read": 1,
            "hash": self.build_message_hash(user_uid, mall_id, request_id),
        }
        if chat_type:
            message["chat_type"] = chat_type
            if chat_type == "conciliation":
                message["to"]["role"] = "session"
        if message_fields:
            message.update(message_fields)

        command = {
            "cmd": "send_message",
            "anti_content": anti_content,
            "request_id": request_id,
            "message": message,
            "random": self.build_send_random(
                mall_id=mall_id,
                global_uid=global_uid,
                user_uid=user_uid,
                content=content,
            ),
        }
        if force_send_sensitive is not None:
            command["force_send_sensitive"] = force_send_sensitive
        if command_fields:
            command.update(command_fields)
        return command

    def build_chat_command_body(self, command, *, client="WEB", anti_content=None, referer=None):
        command = dict(command)
        if "anti_content" not in command:
            command["anti_content"] = anti_content or self.login.get_latest_anti_content(referer or self.CHAT_REFERER)
        outer_anti_content = anti_content or self.login.get_latest_anti_content(referer or self.CHAT_REFERER)
        return {
            "data": command,
            "client": client,
            "anti_content": outer_anti_content,
        }

    def send_chat_command(
        self,
        command,
        *,
        client="WEB",
        anti_content=None,
        referer=None,
        timeout=15,
        path=None,
    ):
        body = self.build_chat_command_body(
            command,
            client=client,
            anti_content=anti_content,
            referer=referer,
        )
        response = self._send(
            "POST",
            path or f"/plateau/chat/{command['cmd']}",
            referer=referer,
            timeout=timeout,
            include_anti_content=False,
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            data=json_request_data(body),
        )
        result = json_or_text(response)
        self._raise_for_api_failure(result)
        return self._unwrap_result(result)

    @staticmethod
    def _raise_for_api_failure(data):
        if not isinstance(data, dict):
            return
        if data.get("success") is False or data.get("result") == "fail":
            message = (
                data.get("error_msg")
                or data.get("errorMsg")
                or data.get("message")
                or data.get("msg")
                or data.get("bizErrorMsg")
                or "api request failed"
            )
            code = data.get("error_code") or data.get("errorCode") or data.get("code")
            raise PddApiError(message, error_code=code, response=data)

    @staticmethod
    def _client_type_value(client):
        if isinstance(client, int):
            return client
        values = {
            "WEB": 1,
            "IOS": 2,
            "ANDROID": 3,
            "MINIAPP": 4,
            "WIN": 5,
            "MAC": 6,
            "HARMONY": 9,
        }
        return values.get(str(client or "WEB").upper(), client)

    @classmethod
    def _is_service_chat_type(cls, chat_type_id):
        if chat_type_id in (None, ""):
            return False
        try:
            return int(chat_type_id) == cls.SERVICE_CHAT_TYPE_ID
        except (TypeError, ValueError):
            return str(chat_type_id) == str(cls.SERVICE_CHAT_TYPE_ID)

    @classmethod
    def build_client_msg_id(cls, conv_id):
        suffix = hashlib.md5(os.urandom(16), usedforsecurity=False).hexdigest()[:7]
        return f"{conv_id}-{int(time.time() * 1000)}-{suffix}"

    def build_text_message_api_payload(
        self,
        user_uid,
        content,
        *,
        token_result=None,
        mall_id=None,
        global_uid=None,
        conv_id=None,
        chat_type_id=None,
        client_msg_id=None,
        msg_type=0,
        message_fields=None,
        force_send_sensitive=None,
        anti_content=None,
        referer=None,
    ):
        if user_uid in (None, ""):
            raise ValueError("user_uid is required")

        mall_id, global_uid = self.resolve_chat_identity(
            token_result,
            mall_id=mall_id,
            global_uid=global_uid,
        )
        user_uid = str(user_uid)
        if conv_id in (None, ""):
            raise ValueError("conv_id is required for service chat send_message")
        conv_id = str(conv_id)
        chat_type_id = chat_type_id if chat_type_id not in (None, "") else 9
        client_msg_id = str(client_msg_id or self.build_client_msg_id(conv_id))
        anti_content = anti_content or self.login.get_latest_anti_content(referer or self.CHAT_REFERER)

        message = {
            "to": {},
            "from": {
                "uid": str(global_uid or ""),
                "user_type": 2,
                "host_id": str(mall_id),
            },
            "chat_type_id": int(chat_type_id),
            "conv_id": conv_id,
            "type": msg_type or 0,
            "client_msg_id": client_msg_id,
            "content": "" if content is None else str(content),
        }
        if force_send_sensitive is not None:
            message["force_send_sensitive"] = force_send_sensitive
        if message_fields:
            message.update(message_fields)
        return {
            "anti_content": anti_content,
            "chat_type_id": int(chat_type_id),
            "client_msg_id": client_msg_id,
            "conv_id": conv_id,
            "message": message,
        }

    def send_text_message_api(
        self,
        user_uid,
        content,
        *,
        token_result=None,
        mall_id=None,
        global_uid=None,
        conv_id=None,
        chat_type_id=None,
        client_msg_id=None,
        msg_type=0,
        message_fields=None,
        force_send_sensitive=None,
        client="WEB",
        referer=None,
        timeout=15,
    ):
        data_payload = self.build_text_message_api_payload(
            user_uid,
            content,
            token_result=token_result,
            mall_id=mall_id,
            global_uid=global_uid,
            conv_id=conv_id,
            chat_type_id=chat_type_id,
            client_msg_id=client_msg_id,
            msg_type=msg_type,
            message_fields=message_fields,
            force_send_sensitive=force_send_sensitive,
            referer=referer,
        )
        body = {
            "data": data_payload,
        }
        response = self._send(
            "POST",
            self.SEND_MESSAGE_PATH,
            referer=referer,
            timeout=timeout,
            include_anti_content=False,
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            data=json_request_data(body),
        )
        result = json_or_text(response)
        self._raise_for_api_failure(result)
        return self._unwrap_result(result)

    def send_text_message(
        self,
        user_uid,
        content,
        *,
        token_result=None,
        mall_id=None,
        global_uid=None,
        chat_type=None,
        msg_type=0,
        message_fields=None,
        command_fields=None,
        force_send_sensitive=None,
        client="WEB",
        referer=None,
        timeout=15,
        conv_id=None,
        chat_type_id=None,
        client_msg_id=None,
    ):
        use_service_api = (
            not command_fields
            and not chat_type
            and self._is_service_chat_type(chat_type_id)
        )
        if use_service_api:
            return self.send_text_message_api(
                user_uid,
                content,
                token_result=token_result,
                mall_id=mall_id,
                global_uid=global_uid,
                conv_id=conv_id,
                chat_type_id=chat_type_id,
                client_msg_id=client_msg_id,
                msg_type=msg_type,
                message_fields=message_fields,
                force_send_sensitive=force_send_sensitive,
                client=client,
                referer=referer,
                timeout=timeout,
            )

        command = self.build_text_message_command(
            user_uid,
            content,
            token_result=token_result,
            mall_id=mall_id,
            global_uid=global_uid,
            chat_type=chat_type,
            msg_type=msg_type,
            message_fields=message_fields,
            command_fields=command_fields,
            force_send_sensitive=force_send_sensitive,
            referer=referer,
        )
        return self.send_chat_command(
            command,
            client=client,
            referer=referer,
            timeout=timeout,
            path=self.SEND_MESSAGE_PATH,
        )

    def send_text_message_to_event(self, event, content, **kwargs):
        user_uid = self.extract_customer_uid(event)
        if not user_uid:
            raise ValueError(f"cannot find customer uid from event={event!r}")
        return self.send_text_message(user_uid, content, **kwargs)

    def pre_upload_image(self, *, chat_type_id=1, referer=None, timeout=15):
        body = {
            "chat_type_id": chat_type_id,
            "file_usage": 1,
        }
        response = self._send(
            "POST",
            self.PRE_UPLOAD_PATH,
            referer=referer,
            timeout=timeout,
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            data=json_request_data(body),
        )
        result = json_or_text(response)
        self._raise_for_api_failure(result)
        return self._unwrap_result(result)

    def upload_image_to_server(self, image_base64, upload_sign, upload_url=None, *, referer=None, timeout=30):
        upload_url = upload_url or "https://file.pinduoduo.com/v2/store_image"
        body = {
            "image": image_base64,
            "upload_sign": upload_sign,
        }
        headers = self._headers(
            referer=referer or self.BASE_URL + "/",
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
                "origin": self.BASE_URL,
                "sec-fetch-site": "same-site",
            },
            include_anti_content=False,
        )
        response = requests.post(
            upload_url,
            headers=headers,
            data=json_request_data(body),
            timeout=timeout,
        )
        response.raise_for_status()
        return json_or_text(response)

    def send_image_message(
        self,
        user_uid,
        image_base64,
        *,
        token_result=None,
        mall_id=None,
        global_uid=None,
        chat_type=None,
        message_fields=None,
        command_fields=None,
        client="WEB",
        referer=None,
        timeout=30,
    ):
        pre_upload_result = self.pre_upload_image(referer=referer, timeout=timeout)
        upload_sign = pre_upload_result["upload_signature"]
        upload_url = pre_upload_result.get("upload_url") or "https://file.pinduoduo.com/v2/store_image"

        upload_result = self.upload_image_to_server(
            image_base64,
            upload_sign,
            upload_url=upload_url,
            referer=referer,
            timeout=timeout,
        )
        image_url = upload_result.get("url") or upload_result.get("image_url") or ""
        if not image_url:
            raise RuntimeError(f"image upload failed, no url in response: {upload_result!r}")

        width = upload_result.get("width") or 0
        height = upload_result.get("height") or 0

        thumb = _build_image_thumb(image_base64, max_size=72)
        image_content = image_base64
        if "," in image_content:
            image_content = image_content.split(",", 1)[1]

        img_message_fields = {
            "size": {
                "height": int(height),
                "width": int(width),
                "image_size": 1,
            },
            "info": {
                "thumb_data": thumb,
            },
        }
        all_message_fields = dict(img_message_fields)
        if message_fields:
            all_message_fields.update(message_fields)

        command = self.build_text_message_command(
            user_uid,
            image_url,
            token_result=token_result,
            mall_id=mall_id,
            global_uid=global_uid,
            chat_type=chat_type,
            msg_type=1,
            message_fields=all_message_fields,
            command_fields=command_fields,
            referer=referer,
        )
        return self.send_chat_command(
            command,
            client=client,
            referer=referer,
            timeout=timeout,
            path=self.SEND_MESSAGE_PATH,
        ), image_url

    @staticmethod
    def extract_customer_uid(event):
        if not isinstance(event, dict):
            return ""

        candidates = []
        if isinstance(event.get("messages"), list):
            candidates.extend(event["messages"])
        nested_event = event.get("event")
        if isinstance(nested_event, dict) and isinstance(nested_event.get("messages"), list):
            candidates.extend(nested_event["messages"])
        if isinstance(event.get("message"), dict):
            candidates.append(event)

        for item in candidates:
            message = item.get("message") if isinstance(item, dict) else None
            if not isinstance(message, dict):
                continue
            sender = message.get("from") or {}
            recipient = message.get("to") or {}
            if sender.get("role") == "user" and sender.get("uid") not in (None, ""):
                return str(sender.get("uid"))
            if recipient.get("role") == "user" and recipient.get("uid") not in (None, ""):
                return str(recipient.get("uid"))
        return ""

    @staticmethod
    def _build_text_decoder():
        from openkefu.platforms.pdd.chat.spider_font_decoder import SpiderFontDecoder

        if SPIDER_FONT_SOURCE:
            return SpiderFontDecoder.from_font(SPIDER_FONT_SOURCE).decode_text
        return SpiderFontDecoder().decode_text

    def get_version_html(self):
        return self._request_text(
            "GET",
            "/chat-merchant/index.html",
            headers={
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                ),
                "cache-control": "max-age=0",
                "priority": "u=0, i",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
            include_anti_content=False,
        )

    @classmethod
    def parse_version(cls, html):
        html = html or ""

        match = cls.SERVICE_BUILD_TIME_RE.search(html)
        if match:
            return match.group("version")

        match = cls.CHAT_VERSION_RE.search(html)
        if match:
            version = match.group("version")
            date, hour, minute, _second = version.split(".")
            return f"{date}{hour}{minute}"

        match = cls.ASSET_VERSION_RE.search(html)
        if match:
            return match.group("version")[:12]

        return cls.DEFAULT_SERVICE_BUILD_TIME

    def get_version(self):
        return self.parse_version(self.get_version_html())

    def _call_sec_websocket_js(self, action, payload=None):
        try:
            proc = subprocess.run(
                ["node", str(self.SEC_WS_JS), action],
                input=json.dumps(payload or {}, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"sec websocket JS bridge timed out: action={action}") from exc
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return json.loads(proc.stdout)

    def build_websocket_info(self, access_token, *, version, ws_base_url=None):
        payload = {
            "token": access_token,
            "serviceBuildTime": version,
            "wsBaseUrl": ws_base_url or self.WS_BASE_URL,
        }

        key_result = self._call_sec_websocket_js("url", payload)
        params = parse_qs(urlparse(key_result["webSocketUrl"]).query)
        return {
            "access_token": access_token,
            "sec_websocket_key": key_result["secWebSocketKey"],
            "websocket_url": key_result["webSocketUrl"],
            "params": {key: values[-1] for key, values in params.items()},
        }

    def build_websocket_info_by_cookies(self, *, version, ws_base_url=None):
        payload = {
            "cookies": self._cookies(),
            "serviceBuildTime": version,
            "wsBaseUrl": ws_base_url or self.WS_BASE_URL,
        }

        key_result = self._call_sec_websocket_js("token", payload)
        params = parse_qs(urlparse(key_result["webSocketUrl"]).query)
        return {
            "access_token": key_result["accessToken"],
            "sec_websocket_key": key_result["secWebSocketKey"],
            "websocket_url": key_result["webSocketUrl"],
            "params": {key: values[-1] for key, values in params.items()},
            "token_response": key_result.get("response"),
        }

    def websocket_headers(self, websocket_info):
        return {
            "Upgrade": "websocket",
            "Origin": self.BASE_URL,
            "Cache-Control": "no-cache",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Pragma": "no-cache",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": websocket_info["sec_websocket_key"],
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "User-Agent": self.WS_USER_AGENT,
        }

    def listen_once(self, *, access_token, version, ws_base_url=None, timeout=20):
        return self.listen_until_account_relogin_required(
            access_token=access_token,
            version=version,
            ws_base_url=ws_base_url,
            timeout=timeout,
        )

    def listen_until_account_relogin_required(self, *, access_token, version, ws_base_url=None, timeout=20):
        websocket_info = self.build_websocket_info(
            access_token,
            version=version,
            ws_base_url=ws_base_url,
        )
        message = self._recv_websocket_message_until(
            websocket_info["websocket_url"],
            self.websocket_headers(websocket_info),
            self.is_account_relogin_required_message,
            timeout=timeout,
        )
        return {
            "websocket": websocket_info,
            "message": message,
        }

    def start_account_relogin_listener(
        self,
        *,
        access_token,
        version,
        ws_base_url=None,
        timeout=20,
        on_message=None,
        on_error=None,
        daemon=False,
    ):
        state = {
            "result": None,
            "error": None,
        }

        def worker():
            try:
                state["result"] = self.listen_until_account_relogin_required(
                    access_token=access_token,
                    version=version,
                    ws_base_url=ws_base_url,
                    timeout=timeout,
                )
                if on_message:
                    on_message(state["result"])
            except Exception as exc:
                state["error"] = exc
                if on_error:
                    on_error(exc)

        thread = threading.Thread(
            target=worker,
            name="account-relogin-listener",
            daemon=daemon,
        )
        state["thread"] = thread
        thread.start()
        return state

    def build_titan_config(
        self,
        *,
        access_token=None,
        token_result=None,
        uid=None,
        titan_access_token=None,
    ):
        uid = str(uid or self.get_realtime_uid(token_result) or "")
        titan_access_token = titan_access_token or self.get_titan_auth_token() or access_token
        if not titan_access_token:
            raise RuntimeError("titan access token is empty")
        config = TitanClientConfig(
            access_token=titan_access_token,
            uid=uid,
            titan_id=uid,
            app_id=2,
            ua=self.WS_USER_AGENT,
            text_decoder=self._build_text_decoder(),
        )
        return config

    def start_titan_listener(
        self,
        *,
        access_token,
        token_result=None,
        uid=None,
        titan_access_token=None,
        sync_all=True,
        pfb_report=True,
        pfb_active_interval=None,
        pfb_idle_interval=None,
        on_send=None,
        on_receive=None,
        on_error=None,
        on_pfb_report=None,
        on_pfb_error=None,
        daemon=False,
    ):
        state = {
            "result": None,
            "error": None,
            "client": None,
            "pfb_thread": None,
            "pfb_last_report": None,
            "pfb_error": None,
            "ready_event": threading.Event(),
        }
        pfb_stop_event = threading.Event()
        pfb_activity_event = threading.Event()
        pfb_lock = threading.Lock()
        pfb_timing = {
            "last_user_message_at": 0.0,
            "last_report_at": 0.0,
        }

        def random_interval(interval, fallback):
            value = interval if interval is not None else fallback
            if isinstance(value, (int, float)):
                seconds = float(value)
                spread = max(1.0, seconds * 0.15)
                return random.uniform(max(1.0, seconds - spread), seconds + spread)
            low, high = value
            low = max(1.0, float(low))
            high = max(low, float(high))
            return random.uniform(low, high)

        def mark_pfb_activity():
            with pfb_lock:
                pfb_timing["last_user_message_at"] = time.monotonic()
            pfb_activity_event.set()

        def start_pfb_reporter():
            if not pfb_report:
                return None

            def pfb_worker():
                next_report_at = time.monotonic() + random_interval(
                    pfb_idle_interval,
                    self.PFB_IDLE_INTERVAL_SECONDS,
                )
                while not pfb_stop_event.is_set():
                    wait_seconds = max(0.0, next_report_at - time.monotonic())
                    if pfb_activity_event.wait(wait_seconds):
                        pfb_activity_event.clear()
                        with pfb_lock:
                            last_user_message_at = pfb_timing["last_user_message_at"]
                            last_report_at = pfb_timing["last_report_at"]
                        if last_user_message_at > last_report_at:
                            active_report_at = last_user_message_at + random_interval(
                                pfb_active_interval,
                                self.PFB_ACTIVE_INTERVAL_SECONDS,
                            )
                            next_report_at = min(next_report_at, active_report_at)
                        continue

                    try:
                        with self._http_lock:
                            result = self.login.report_pfb_a2()
                    except Exception as exc:
                        state["pfb_error"] = exc
                        if on_pfb_error:
                            on_pfb_error(exc)
                    else:
                        with pfb_lock:
                            pfb_timing["last_report_at"] = time.monotonic()
                        state["pfb_last_report"] = result
                        if on_pfb_report:
                            on_pfb_report(result)

                    with pfb_lock:
                        has_unreported_user_message = (
                            pfb_timing["last_user_message_at"] > pfb_timing["last_report_at"]
                        )
                    fallback = (
                        self.PFB_ACTIVE_INTERVAL_SECONDS
                        if has_unreported_user_message
                        else self.PFB_IDLE_INTERVAL_SECONDS
                    )
                    configured = pfb_active_interval if has_unreported_user_message else pfb_idle_interval
                    next_report_at = time.monotonic() + random_interval(configured, fallback)

            thread = threading.Thread(
                target=pfb_worker,
                name="pfb-a2-reporter",
                daemon=daemon,
            )
            state["pfb_thread"] = thread
            thread.start()
            return thread

        def stop_pfb_reporter():
            pfb_stop_event.set()
            pfb_activity_event.set()
            thread = state.get("pfb_thread")
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)

        def emit_send(payload):
            if on_send:
                on_send(payload)

        def emit_receive(payload):
            event = payload.get("event") if isinstance(payload, dict) else None
            if self._titan_event_has_user_message(event):
                mark_pfb_activity()
            if on_receive:
                on_receive(payload)

        def worker():
            try:
                config = self.build_titan_config(
                    access_token=access_token,
                    token_result=token_result,
                    uid=uid,
                    titan_access_token=titan_access_token,
                )
                titan = TitanWebSocketClient(config)
                state["client"] = titan
                original_send_raw = titan._send_raw

                def traced_send_raw(payload):
                    emit_send(self.summarize_titan_upstream(payload))
                    original_send_raw(payload)

                titan._send_raw = traced_send_raw
                with titan:
                    start_pfb_reporter()
                    emit_receive({
                        "type": "session",
                        "frame": summarize_downstream(titan.last_session),
                    })

                    if sync_all:
                        sync_frame = titan.send_sync_all(wait_response=True)
                        emit_receive({
                            "type": "sync",
                            "frame": summarize_downstream(sync_frame),
                        })
                    state["ready_event"].set()

                    while True:
                        event = titan.recv_event(timeout=None)
                        emit_receive({
                            "type": "event",
                            "event": event,
                        })
            except Exception as exc:
                client = state.get("client")
                if getattr(client, "close_requested", False):
                    state["result"] = {"status": "closed"}
                else:
                    state["error"] = exc
                    ready_event = state.get("ready_event")
                    if ready_event and not ready_event.is_set():
                        state["ready_error"] = exc
                        ready_event.set()
                    if on_error:
                        on_error(exc)
            finally:
                stop_pfb_reporter()

        thread = threading.Thread(
            target=worker,
            name="titan-ws-listener",
            daemon=daemon,
        )
        state["thread"] = thread
        thread.start()
        return state

    @staticmethod
    def _titan_event_has_user_message(event):
        from openkefu.platforms.pdd.chat.auto_reply import incoming_user_messages

        return bool(incoming_user_messages({"type": "event", "event": event}))

    @staticmethod
    def summarize_titan_upstream(payload):
        frame = decode_titan_frame(payload, decode_downstream=False)
        summary = {
            "magic": frame.magic,
            "cmd": frame.cmd,
            "ctx": frame.ctx,
            "reserve": frame.reserve,
            "bodyLen": frame.body_len,
        }
        if frame.payload_bytes:
            upstream = decode_message(frame.payload_bytes, TITAN_UPSTREAM_SCHEMA)
            summary["payload"] = CustomerServiceClient._json_safe(upstream)
        return summary

    @staticmethod
    def _json_safe(value):
        if isinstance(value, bytes):
            return {
                "bytes": len(value),
                "preview": value[:32].hex(),
            }
        if isinstance(value, dict):
            return {key: CustomerServiceClient._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [CustomerServiceClient._json_safe(item) for item in value]
        return value

    @staticmethod
    def is_account_relogin_required_message(message):
        if not isinstance(message, dict):
            return False
        payload = message.get("message") or {}
        return (
            message.get("response") == "system_push"
            and payload.get("type") == 30
            and payload.get("from", {}).get("uid") == -1
            and "账户在别处登录" in str(payload.get("content", ""))
        )

    def _recv_websocket_message_until(self, websocket_url, headers, should_stop, *, timeout=20):
        parsed = urlparse(websocket_url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported websocket scheme: {parsed.scheme}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        sock.settimeout(timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)

        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            request_headers = {
                "Host": parsed.netloc,
                **headers,
            }
            request = "\r\n".join(
                [f"GET {path} HTTP/1.1"]
                + [f"{name}: {value}" for name, value in request_headers.items() if value]
                + ["", ""]
            )
            sock.sendall(request.encode("utf-8"))
            self._read_handshake_response(sock)
            sock.settimeout(None)

            while True:
                frame = self._read_ws_frame(sock)
                if frame["opcode"] == 0x1:
                    payload = self._decode_ws_payload(frame)
                    message = self._json_or_text(payload.decode("utf-8", errors="replace"))
                    if should_stop(message):
                        return message
                if frame["opcode"] == 0x2:
                    payload = self._decode_ws_payload(frame)
                    message = self._json_or_text(payload.decode("utf-8", errors="replace"))
                    if should_stop(message):
                        return message
                if frame["opcode"] == 0x8:
                    raise ConnectionError(
                        "websocket closed before account relogin message: "
                        f"{frame['payload'].decode('utf-8', errors='replace')}"
                    )
                if frame["opcode"] == 0x9:
                    self._send_ws_frame(sock, frame["payload"], opcode=0xA)
        finally:
            try:
                self._send_ws_frame(sock, b"", opcode=0x8)
            except OSError:
                pass
            sock.close()

    @staticmethod
    def _read_exact(sock, size):
        chunks = []
        remaining = size
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ConnectionError("websocket connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def _read_handshake_response(cls, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            data += cls._read_exact(sock, 1)
        text = data.decode("iso-8859-1", errors="replace")
        status_line = text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise ConnectionError(f"websocket handshake failed: {status_line}")
        return text

    @classmethod
    def _read_ws_frame(cls, sock):
        first, second = cls._read_exact(sock, 2)
        compressed = bool(first & 0x40)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", cls._read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", cls._read_exact(sock, 8))[0]

        mask = cls._read_exact(sock, 4) if masked else b""
        payload = cls._read_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return {
            "compressed": compressed,
            "opcode": opcode,
            "payload": payload,
        }

    @staticmethod
    def _decode_ws_payload(frame):
        payload = frame["payload"]
        if not frame.get("compressed"):
            return payload
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        return decompressor.decompress(payload + b"\x00\x00\xff\xff") + decompressor.flush()

    @staticmethod
    def _json_or_text(text):
        try:
            return json.loads(text)
        except ValueError:
            return text

    @staticmethod
    def _send_ws_frame(sock, payload, *, opcode):
        payload = payload or b""
        mask = os.urandom(4)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length <= 0xFFFF:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(header + mask + masked)


def _build_image_thumb(image_base64, max_size=72):
    try:
        import base64
        import io
        from PIL import Image
    except ImportError:
        return image_base64

    data = image_base64
    if "," in str(data):
        data = str(data).split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if w > 0 and h > 0:
            scale = min(max_size / w, max_size / h)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            thumb = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            thumb.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
    except Exception:
        pass
    return image_base64
