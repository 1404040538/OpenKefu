from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from openkefu.platforms.pdd.chat.titan_codec import (
    ACK_COMMAND,
    CMD_TITAN,
    DEFAULT_HOST,
    DEFAULT_TITAN_URL,
    DEFAULT_USER_AGENT,
    MAGIC_UPSTREAM,
    NOTIFY_COMMAND,
    NOTIFY_DATA_COMMAND,
    NOTIFY_DATA_LITE_COMMAND,
    NOTIFY_INNER_COMMAND,
    PING_COMMAND,
    SESSION_COMMAND,
    SYNC_COMMAND,
    TOKEN_EXPIRED_CODES,
    TitanFrame,
    build_ack_frame,
    build_heartbeat_frame,
    build_notify_data_lite_ack_frame,
    build_pong_frame,
    build_session_frame,
    build_sync_frame,
    decode_notify,
    decode_notify_data,
    decode_notify_data_lite,
    decode_notify_inner,
    decode_sync_response,
    decode_titan_frame,
    random_titan_id,
    summarize_downstream,
)


@dataclass
class TitanClientConfig:
    access_token: str
    uid: str = ""
    app_id: int = 2
    titan_id: str = ""
    titan_id_prefix: str = ""
    url: str = DEFAULT_TITAN_URL
    host: str = DEFAULT_HOST
    ua: str = DEFAULT_USER_AGENT
    os: int = 5
    auth_type: int | None = 4
    scene_type: int | None = 1001
    custom_payload: dict[str, Any] | None = None
    heartbeat_interval: float = 45.0
    timeout: float = 15.0
    websocket_headers: dict[str, str] = field(default_factory=dict)
    text_decoder: Callable[[str], str] | None = None

    def normalized_titan_id(self) -> str:
        return self.titan_id or random_titan_id(self.titan_id_prefix)


class TitanWebSocketClient:
    def __init__(self, config: TitanClientConfig):
        self.config = config
        self.ctx = 100
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self.status = "closed"
        self.titan_id = config.normalized_titan_id()
        self.last_send_at = 0.0
        self.last_session: TitanFrame | None = None
        self.msg_offset_map: dict[int, int] = {}
        self.close_requested = False

    def __enter__(self) -> "TitanWebSocketClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> TitanFrame:
        self.close_requested = False
        sock = None
        try:
            sock = self._open_websocket()
            if self.close_requested:
                sock.close()
                self.status = "closed"
                raise ConnectionError("websocket closed by local request")
            self.sock = sock
            self.status = "connecting"
            session_ctx = self._next_ctx()
            self._send_raw(
                build_session_frame(
                    app_id=self.config.app_id,
                    access_token=self.config.access_token,
                    uid=self.config.uid,
                    titan_id=self.titan_id,
                    ctx=session_ctx,
                    host=self.config.host,
                    ua=self.config.ua,
                    os=self.config.os,
                    custom_payload=self.config.custom_payload,
                    auth_type=self.config.auth_type,
                    scene_type=self.config.scene_type,
                )
            )
            frame = self.recv_until_ctx(session_ctx, timeout=self.config.timeout)
            payload = frame.payload or {}
            error_code = int(payload.get("errorCode") or 0)
            self.last_session = frame
            if error_code:
                message = payload.get("bizErrorMsg") or "titan.session failed"
                if error_code in TOKEN_EXPIRED_CODES:
                    raise PermissionError(f"{message}: errorCode={error_code}")
                raise ConnectionError(f"{message}: errorCode={error_code}")
            self.status = "open"
            return frame
        except Exception:
            # 握手/会话校验失败时关闭已建立的 socket，避免 FD 泄漏
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if self.sock is sock:
                self.sock = None
            self.status = "closed"
            raise

    def send_sync_all(self, *, wait_response: bool = True) -> TitanFrame | int:
        self._ensure_open()
        ctx = self._next_ctx()
        self._send_raw(
            build_sync_frame(
                app_id=self.config.app_id,
                ctx=ctx,
                host=self.config.host,
                sync_all=True,
            )
        )
        if not wait_response:
            return ctx
        return self.recv_until_ctx(ctx, timeout=self.config.timeout)

    def recv_event(self, *, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.maybe_send_heartbeat()
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise TimeoutError("timed out waiting for titan event")
            try:
                frame = self.recv_frame(timeout=min(1.0, remaining) if remaining else 1.0)
            except TimeoutError:
                continue
            event = self.handle_frame(frame)
            if event is not None:
                return event

    def listen(
        self,
        on_event: Callable[[dict[str, Any]], None],
        *,
        duration: float | None = None,
    ) -> None:
        deadline = None if duration is None else time.monotonic() + duration
        while self.status == "open":
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                event = self.recv_event(timeout=1.0)
            except TimeoutError:
                continue
            on_event(event)

    def recv_until_ctx(self, ctx: int, *, timeout: float | None = None) -> TitanFrame:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise TimeoutError(f"timed out waiting for titan ctx={ctx}")
            frame = self.recv_frame(timeout=remaining)
            if frame.ctx == ctx:
                return frame
            self.handle_frame(frame)

    def recv_frame(self, *, timeout: float | None = None) -> TitanFrame:
        sock = self.sock
        if sock is None:
            raise ConnectionError("websocket is not connected")
        previous_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            while True:
                ws_frame = self._read_ws_frame(sock)
                opcode = ws_frame["opcode"]
                if opcode == 0x8:
                    self.status = "closed"
                    raise ConnectionError("websocket closed by remote")
                if opcode == 0x9:
                    self._send_ws_frame(sock, ws_frame["payload"], opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                if opcode not in {0x1, 0x2}:
                    continue
                payload = self._decode_ws_payload(ws_frame)
                return decode_titan_frame(payload)
        except socket.timeout as exc:
            raise TimeoutError("timed out waiting for websocket frame") from exc
        finally:
            try:
                sock.settimeout(previous_timeout)
            except OSError:
                pass

    def handle_frame(self, frame: TitanFrame) -> dict[str, Any] | None:
        payload = frame.payload or {}
        command = payload.get("command")
        body = payload.get("body")

        if frame.magic == MAGIC_UPSTREAM and frame.cmd == CMD_TITAN and command == PING_COMMAND:
            self.send_pong()
            return {
                "type": "system",
                "command": PING_COMMAND,
                "response": "pong",
                "frame": summarize_downstream(frame),
            }

        if command == NOTIFY_DATA_LITE_COMMAND and body:
            decoded = decode_notify_data_lite(body)
            self.ack_notify_data_lite(decoded)
            payload = decoded.get("decodedPayload")
            return {
                "type": "message",
                "command": command,
                "actionId": decoded.get("bizType"),
                "payload": payload,
                "messages": extract_chat_messages(payload, text_decoder=self.config.text_decoder),
                "decoded": decoded,
                "frame": summarize_downstream(frame),
            }

        if command == NOTIFY_COMMAND and body:
            decoded = decode_notify(body)
            self._send_sync_for_notify(decoded)
            return {
                "type": "notify",
                "command": command,
                "decoded": decoded,
                "frame": summarize_downstream(frame),
            }

        if command == NOTIFY_DATA_COMMAND and body:
            decoded = decode_notify_data(body)
            self._ack_group_messages(decoded, force_ack=True)
            return {
                "type": "notifyData",
                "command": command,
                "decoded": decoded,
                "frame": summarize_downstream(frame),
            }

        if command == NOTIFY_INNER_COMMAND and body:
            decoded = decode_notify_inner(body)
            return {
                "type": "notifyInner",
                "command": command,
                "decoded": decoded,
                "frame": summarize_downstream(frame),
            }

        if command == SYNC_COMMAND and body:
            decoded = decode_sync_response(body)
            self._ack_group_messages(decoded, force_ack=bool(decoded.get("needAck")))
            return {
                "type": "sync",
                "command": command,
                "decoded": decoded,
                "frame": summarize_downstream(frame),
            }

        if command == ACK_COMMAND:
            return {
                "type": "ack",
                "command": command,
                "frame": summarize_downstream(frame),
            }

        if command or payload:
            return {
                "type": "frame",
                "command": command,
                "frame": summarize_downstream(frame),
            }
        return None

    def send_pong(self) -> None:
        self._ensure_socket()
        self._send_raw(
            build_pong_frame(
                app_id=self.config.app_id,
                ctx=self._next_ctx(),
                host=self.config.host,
            )
        )

    def ack_notify_data_lite(self, decoded: dict[str, Any]) -> None:
        msg_id = decoded.get("msgId")
        biz_type = decoded.get("bizType")
        if not msg_id or biz_type is None:
            return
        self._send_raw(
            build_notify_data_lite_ack_frame(
                app_id=self.config.app_id,
                ctx=self._next_ctx(),
                msg_id=msg_id,
                biz_type=int(biz_type),
                host=self.config.host,
                additional_map=decoded.get("additionalMap"),
            )
        )

    def _send_sync_for_notify(self, decoded: dict[str, Any]) -> None:
        uid_offsets = {
            int(group_id): int(self.msg_offset_map.get(int(group_id), 0))
            for group_id in decoded.get("uidGroupList") or []
        }
        titanid_offsets = {
            int(group_id): int(self.msg_offset_map.get(int(group_id), 0))
            for group_id in decoded.get("titanidGroupList") or []
        }
        if not uid_offsets and not titanid_offsets:
            return
        self._send_raw(
            build_sync_frame(
                app_id=self.config.app_id,
                ctx=self._next_ctx(),
                host=self.config.host,
                sync_all=False,
                uid_offset_map=uid_offsets,
                titanid_offset_map=titanid_offsets,
            )
        )

    def _ack_group_messages(self, decoded: dict[str, Any], *, force_ack: bool) -> None:
        ack = {
            "uidMap": {},
            "titanidMap": {},
        }
        for source_name, ack_name in (("uidMap", "uidMap"), ("titanidMap", "titanidMap")):
            for group_id, group in (decoded.get(source_name) or {}).items():
                msg_list = group.get("msgList") or []
                if not msg_list:
                    continue
                max_offset = int(msg_list[-1].get("offset") or 0)
                self.msg_offset_map[int(group_id)] = max_offset
                if not force_ack:
                    continue
                details = {}
                for msg in msg_list:
                    offset = int(msg.get("offset") or 0)
                    details[offset] = {
                        "bizType": int(msg.get("bizType") or 0),
                        "subType": int(msg.get("subType") or 0),
                        "msgId": str(msg.get("msgId") or ""),
                        "timestamp": int(msg.get("timestamp") or 0),
                    }
                ack[ack_name][int(group_id)] = {
                    "clientOffset": max_offset,
                    "msgDetailMap": details,
                }
        if ack["uidMap"] or ack["titanidMap"]:
            self._send_raw(
                build_ack_frame(
                    app_id=self.config.app_id,
                    ctx=self._next_ctx(),
                    host=self.config.host,
                    ack=ack,
                )
            )

    def maybe_send_heartbeat(self) -> bool:
        if self.status != "open":
            return False
        if time.monotonic() - self.last_send_at < self.config.heartbeat_interval:
            return False
        self._send_raw(build_heartbeat_frame(ctx=self._next_ctx()))
        return True

    def close(self) -> None:
        self.close_requested = True
        self.status = "closing"
        sock = self.sock
        if not sock:
            self.status = "closed"
            return
        try:
            self._send_ws_frame(sock, b"", opcode=0x8)
        except OSError:
            pass
        try:
            sock.close()
        finally:
            if self.sock is sock:
                self.sock = None
            self.status = "closed"

    def _next_ctx(self) -> int:
        self.ctx += 1
        return self.ctx

    def _send_raw(self, payload: bytes) -> None:
        self._ensure_socket()
        self._send_ws_frame(self.sock, payload, opcode=0x2)
        self.last_send_at = time.monotonic()

    def _ensure_socket(self) -> None:
        if self.sock is None:
            raise ConnectionError("websocket is not connected")

    def _ensure_open(self) -> None:
        self._ensure_socket()
        if self.status != "open":
            raise ConnectionError(f"titan connection is not open: {self.status}")

    def _open_websocket(self) -> socket.socket | ssl.SSLSocket:
        parsed = urlparse(self.config.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"unsupported websocket scheme: {parsed.scheme}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((parsed.hostname, port), timeout=self.config.timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        sock.settimeout(self.config.timeout)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = {
            "Host": parsed.netloc,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": sec_key,
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "Origin": "https://mms.pinduoduo.com",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": self.config.ua,
        }
        headers.update(self.config.websocket_headers)
        request = "\r\n".join(
            [f"GET {path} HTTP/1.1"]
            + [f"{name}: {value}" for name, value in headers.items() if value]
            + ["", ""]
        )
        sock.sendall(request.encode("utf-8"))
        self._read_handshake_response(sock)
        return sock

    @classmethod
    def _read_handshake_response(cls, sock: socket.socket | ssl.SSLSocket) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("websocket closed during handshake")
            data += chunk
        text = data.decode("iso-8859-1", errors="replace")
        status_line = text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise ConnectionError(f"websocket handshake failed: {status_line}")
        return text

    @classmethod
    def _read_exact(cls, sock: socket.socket | ssl.SSLSocket, size: int) -> bytes:
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
    def _read_ws_frame(cls, sock: socket.socket | ssl.SSLSocket) -> dict[str, Any]:
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
    def _decode_ws_payload(frame: dict[str, Any]) -> bytes:
        payload = frame["payload"]
        if not frame.get("compressed"):
            return payload
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        return decompressor.decompress(payload + b"\x00\x00\xff\xff") + decompressor.flush()

    @staticmethod
    def _send_ws_frame(sock: socket.socket | ssl.SSLSocket, payload: bytes, *, opcode: int) -> None:
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


def connect_titan_once(config: TitanClientConfig, *, sync_all: bool = True) -> dict[str, Any]:
    with TitanWebSocketClient(config) as client:
        result: dict[str, Any] = {
            "titanId": client.titan_id,
            "session": summarize_downstream(client.last_session) if client.last_session else None,
        }
        if sync_all:
            sync_frame = client.send_sync_all(wait_response=True)
            result["sync"] = summarize_downstream(sync_frame)
        return result


def dumps_event(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, indent=2, default=str)


def extract_chat_messages(
    payload: Any,
    *,
    text_decoder: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    messages: list[dict[str, Any]] = []
    push_data = payload.get("push_data")
    if isinstance(push_data, dict):
        for item in push_data.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("message"), dict):
                messages.append(
                    {
                        "chat_type_id": item.get("chat_type_id"),
                        "seq_type": push_data.get("seq_type"),
                        "seq_id": push_data.get("seq_id"),
                        "message": _decode_message_content(item["message"], text_decoder),
                    }
                )

    if isinstance(payload.get("message"), dict):
        messages.append(
            {
                "response": payload.get("response"),
                "request_id": payload.get("request_id"),
                "message": _decode_message_content(payload["message"], text_decoder),
            }
        )
    return messages


def _decode_message_content(
    message: dict[str, Any],
    text_decoder: Callable[[str], str] | None,
) -> dict[str, Any]:
    if text_decoder is None:
        return message
    content = message.get("content")
    if not isinstance(content, str):
        return message
    try:
        decoded_content = text_decoder(content)
    except Exception as exc:
        decoded = dict(message)
        decoded["decode_error"] = str(exc)
        return decoded
    if decoded_content == content:
        return message
    decoded = dict(message)
    decoded["decoded_content"] = decoded_content
    return decoded
