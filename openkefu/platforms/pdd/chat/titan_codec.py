from __future__ import annotations

import base64
import json
import random
import string
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any

from openkefu.platforms.pdd.config import TITAN_HOST, TITAN_URL, WS_USER_AGENT

DEFAULT_TITAN_URL = TITAN_URL
DEFAULT_HOST = TITAN_HOST
DEFAULT_USER_AGENT = WS_USER_AGENT

MAGIC_UPSTREAM = 10
MAGIC_HEARTBEAT = 0
CMD_HEARTBEAT = 0
CMD_TITAN = 102
RESERVE_DEFAULT = 1
PROTOCOL_DEFAULT = 1
COMPRESS_NONE = 0
COMPRESS_GZIP = 1

SESSION_COMMAND = "titan.session"
PING_COMMAND = "titan.ping"
PONG_COMMAND = "titan.pong"
SYNC_COMMAND = "titan.sync"
ACK_COMMAND = "titan.ack"
NOTIFY_DATA_LITE_COMMAND = "titan.notifyDataLite"
NOTIFY_DATA_LITE_ACK_COMMAND = "titan.notifyDataLite.ack"
NOTIFY_COMMAND = "titan.notify"
NOTIFY_DATA_COMMAND = "titan.notifyData"
NOTIFY_INNER_COMMAND = "titan.notify.inner"

TOKEN_EXPIRED_CODES = {622, 623, 624, 626, 627, 716}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    repeated: bool = False
    message_schema: dict[int, "FieldSpec"] | None = None
    key_kind: str | None = None
    value_kind: str | None = None
    value_schema: dict[int, "FieldSpec"] | None = None


@dataclass
class TitanFrame:
    magic: int
    cmd: int
    ctx: int
    reserve: int
    body_len: int
    payload_bytes: bytes
    payload: dict[str, Any] | None = None


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("varint data is incomplete")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            raise ValueError("varint is too long")


def _write_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint only supports non-negative integers")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(output)


def _field_key(field_no: int, wire_type: int) -> bytes:
    return _write_varint((field_no << 3) | wire_type)


def _ensure_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, list):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"cannot convert {type(value)!r} to bytes")


def _wire_type_for_kind(kind: str) -> int:
    if kind in {"uint32", "uint64", "bool"}:
        return 0
    if kind in {"string", "bytes", "message", "map"}:
        return 2
    raise ValueError(f"unsupported protobuf kind: {kind}")


def _map_entry_schema(spec: FieldSpec) -> dict[int, FieldSpec]:
    if not spec.key_kind or not spec.value_kind:
        raise ValueError(f"map field {spec.name} is missing key/value specs")
    return {
        1: FieldSpec("key", spec.key_kind),
        2: FieldSpec("value", spec.value_kind, message_schema=spec.value_schema),
    }


def _normalize_map_key(key: Any, kind: str) -> Any:
    if kind in {"uint32", "uint64"}:
        return int(key)
    if kind == "bool":
        return bool(key)
    return str(key)


def _append_field(output: bytearray, field_no: int, spec: FieldSpec, value: Any) -> None:
    if spec.kind in {"uint32", "uint64", "bool"}:
        output.extend(_field_key(field_no, 0))
        output.extend(_write_varint(1 if spec.kind == "bool" and value else int(value)))
        return

    if spec.kind == "string":
        payload = str(value).encode("utf-8")
    elif spec.kind == "bytes":
        payload = _ensure_bytes(value)
    elif spec.kind == "message":
        if isinstance(value, (bytes, bytearray, memoryview)):
            payload = _ensure_bytes(value)
        else:
            if spec.message_schema is None:
                raise ValueError(f"message field {spec.name} is missing schema")
            payload = encode_message(value, spec.message_schema)
    else:
        raise ValueError(f"unsupported field kind for encode: {spec.kind}")

    output.extend(_field_key(field_no, 2))
    output.extend(_write_varint(len(payload)))
    output.extend(payload)


def encode_message(values: dict[str, Any], schema: dict[int, FieldSpec]) -> bytes:
    output = bytearray()
    for field_no, spec in sorted(schema.items()):
        if spec.name not in values or values[spec.name] is None:
            continue
        value = values[spec.name]
        if spec.repeated:
            for item in value or []:
                _append_field(output, field_no, spec, item)
            continue
        if spec.kind == "map":
            entry_schema = _map_entry_schema(spec)
            for key, map_value in (value or {}).items():
                entry = {
                    "key": _normalize_map_key(key, spec.key_kind or "string"),
                    "value": map_value,
                }
                payload = encode_message(entry, entry_schema)
                output.extend(_field_key(field_no, 2))
                output.extend(_write_varint(len(payload)))
                output.extend(payload)
            continue
        _append_field(output, field_no, spec, value)
    return bytes(output)


def _skip_unknown(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _read_varint(data, offset)
        return offset
    if wire_type == 1:
        return offset + 8
    if wire_type == 2:
        length, offset = _read_varint(data, offset)
        return offset + length
    if wire_type == 5:
        return offset + 4
    raise ValueError(f"unsupported protobuf wire type: {wire_type}")


def _read_scalar(data: bytes, offset: int, spec: FieldSpec, wire_type: int) -> tuple[Any, int]:
    if spec.kind in {"uint32", "uint64", "bool"}:
        if wire_type != 0:
            raise ValueError(f"field {spec.name} expected varint, got wire {wire_type}")
        value, offset = _read_varint(data, offset)
        return bool(value) if spec.kind == "bool" else value, offset

    if wire_type != 2:
        raise ValueError(f"field {spec.name} expected length-delimited, got wire {wire_type}")
    length, offset = _read_varint(data, offset)
    chunk = data[offset: offset + length]
    if len(chunk) != length:
        raise ValueError("protobuf length-delimited field is incomplete")
    offset += length

    if spec.kind == "string":
        return chunk.decode("utf-8", errors="replace"), offset
    if spec.kind == "bytes":
        return chunk, offset
    if spec.kind == "message":
        if spec.message_schema is None:
            return chunk, offset
        return decode_message(chunk, spec.message_schema), offset
    raise ValueError(f"unsupported scalar kind: {spec.kind}")


def _read_packed_varints(data: bytes, spec: FieldSpec) -> list[Any]:
    values = []
    offset = 0
    while offset < len(data):
        value, offset = _read_varint(data, offset)
        values.append(bool(value) if spec.kind == "bool" else value)
    return values


def decode_message(data: bytes, schema: dict[int, FieldSpec]) -> dict[str, Any]:
    data = _ensure_bytes(data)
    result: dict[str, Any] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_no = key >> 3
        wire_type = key & 0x07
        spec = schema.get(field_no)
        if spec is None:
            offset = _skip_unknown(data, offset, wire_type)
            continue

        if spec.kind == "map":
            if wire_type != 2:
                offset = _skip_unknown(data, offset, wire_type)
                continue
            length, offset = _read_varint(data, offset)
            chunk = data[offset: offset + length]
            offset += length
            entry = decode_message(chunk, _map_entry_schema(spec))
            if "key" in entry:
                result.setdefault(spec.name, {})[entry["key"]] = entry.get("value")
            continue

        if spec.repeated and wire_type == 2 and spec.kind in {"uint32", "uint64", "bool"}:
            length, offset = _read_varint(data, offset)
            chunk = data[offset: offset + length]
            offset += length
            result.setdefault(spec.name, []).extend(_read_packed_varints(chunk, spec))
            continue

        value, offset = _read_scalar(data, offset, spec, wire_type)
        if spec.repeated:
            result.setdefault(spec.name, []).append(value)
        else:
            result[spec.name] = value
    return result


APP_INFO_SCHEMA = {
    1: FieldSpec("titanid", "string"),
    3: FieldSpec("ua", "string"),
    4: FieldSpec("os", "uint32"),
    5: FieldSpec("uid", "string"),
    11: FieldSpec("repackage", "bool"),
    12: FieldSpec("accesstoken", "string"),
    13: FieldSpec("customPayload", "map", key_kind="string", value_kind="bytes"),
    17: FieldSpec("authType", "uint32"),
    19: FieldSpec("sceneType", "uint32"),
}

EXTENSION_MAP_SCHEMA = {
    1: FieldSpec("info", "map", key_kind="string", value_kind="bytes"),
}

MULTICAST_GROUP_KEY_INFO_SCHEMA = {
    1: FieldSpec("appId", "uint32"),
    2: FieldSpec("bizType", "uint32"),
    3: FieldSpec("groupId", "string"),
}

RECONNECT_INFO_SCHEMA = {
    1: FieldSpec("version", "uint32"),
    2: FieldSpec("delaySecond", "uint32"),
}

TITAN_DOWNSTREAM_SCHEMA = {
    1: FieldSpec("command", "string"),
    2: FieldSpec("protocol", "uint32"),
    3: FieldSpec("errorCode", "uint32"),
    4: FieldSpec("bizCode", "uint32"),
    5: FieldSpec("bizErrorMsg", "string"),
    6: FieldSpec("compress", "uint32"),
    9: FieldSpec("extension", "bytes"),
    10: FieldSpec("body", "bytes"),
    11: FieldSpec("downstreamSeq", "uint64"),
    12: FieldSpec("conId", "uint64"),
    13: FieldSpec("ctxId", "uint64"),
}

TITAN_SESSION_REQUEST_SCHEMA = {
    7: FieldSpec("encryptedAppInfo", "bytes"),
    10: FieldSpec("requestType", "uint32"),
    11: FieldSpec("protocolVersion", "uint32"),
    12: FieldSpec("isPushConn", "bool"),
}

TITAN_UPSTREAM_SCHEMA = {
    1: FieldSpec("appId", "uint32"),
    2: FieldSpec("command", "string"),
    3: FieldSpec("protocol", "uint32"),
    4: FieldSpec("compress", "uint32"),
    6: FieldSpec("host", "string"),
    7: FieldSpec("appinfo", "bytes"),
    8: FieldSpec("sessionResumptionReq", "bytes"),
    9: FieldSpec("body", "bytes"),
    11: FieldSpec("upstreamSeq", "uint64"),
    12: FieldSpec("conId", "uint64"),
    13: FieldSpec("ctxId", "uint64"),
    14: FieldSpec("keyInfo", "message", message_schema=MULTICAST_GROUP_KEY_INFO_SCHEMA),
}

UPDATE_APP_INFO_REQUEST_SCHEMA = {
    1: FieldSpec("appInfo", "bytes"),
}

NOTIFY_DATA_LITE_SCHEMA = {
    1: FieldSpec("uid", "string"),
    2: FieldSpec("titanid", "string"),
    3: FieldSpec("os", "uint32"),
    4: FieldSpec("bizType", "uint32"),
    5: FieldSpec("msgId", "string"),
    6: FieldSpec("payload", "bytes"),
    7: FieldSpec("additionalMap", "map", key_kind="string", value_kind="string"),
    8: FieldSpec("subType", "uint32"),
    9: FieldSpec("timestamp", "uint64"),
}

NOTIFY_DATA_LITE_ACK_SCHEMA = {
    1: FieldSpec("msgId", "string"),
    2: FieldSpec("bizType", "uint32"),
    3: FieldSpec("additionalMap", "map", key_kind="string", value_kind="string"),
    4: FieldSpec("subType", "uint32"),
    5: FieldSpec("timestamp", "uint64"),
}

NOTIFY_SCHEMA = {
    1: FieldSpec("uid", "string"),
    2: FieldSpec("titanid", "string"),
    3: FieldSpec("os", "uint32"),
    4: FieldSpec("uidGroupList", "uint32", repeated=True),
    5: FieldSpec("titanidGroupList", "uint32", repeated=True),
}

NOTIFY_MSG_SCHEMA = {
    1: FieldSpec("bizType", "uint32"),
    2: FieldSpec("isExpired", "bool"),
    3: FieldSpec("offset", "uint64"),
    4: FieldSpec("startOffset", "uint64"),
    5: FieldSpec("endOffset", "uint64"),
    6: FieldSpec("payload", "bytes"),
    7: FieldSpec("msgId", "string"),
    8: FieldSpec("subType", "uint32"),
    9: FieldSpec("timestamp", "uint64"),
    10: FieldSpec("expiredTs", "uint64"),
}

NOTIFY_GROUP_MSG_LIST_SCHEMA = {
    1: FieldSpec("msgList", "message", repeated=True, message_schema=NOTIFY_MSG_SCHEMA),
}

NOTIFY_DATA_SCHEMA = {
    1: FieldSpec("uid", "string"),
    2: FieldSpec("titanid", "string"),
    3: FieldSpec("os", "uint32"),
    4: FieldSpec(
        "uidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=NOTIFY_GROUP_MSG_LIST_SCHEMA,
    ),
    5: FieldSpec(
        "titanidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=NOTIFY_GROUP_MSG_LIST_SCHEMA,
    ),
    6: FieldSpec("additionalMap", "map", key_kind="string", value_kind="string"),
}

SYNC_SCHEMA = {
    1: FieldSpec("uidOffsetMap", "map", key_kind="uint32", value_kind="uint64"),
    2: FieldSpec("titanidOffsetMap", "map", key_kind="uint32", value_kind="uint64"),
    3: FieldSpec("syncAll", "bool"),
    4: FieldSpec("accept", "uint32"),
}

SYNC_MSG_SCHEMA = {
    1: FieldSpec("bizType", "uint32"),
    2: FieldSpec("isExpired", "bool"),
    3: FieldSpec("offset", "uint64"),
    4: FieldSpec("startOffset", "uint64"),
    5: FieldSpec("endOffset", "uint64"),
    6: FieldSpec("payload", "bytes"),
    7: FieldSpec("msgId", "string"),
    8: FieldSpec("subType", "uint32"),
    9: FieldSpec("timestamp", "uint64"),
    10: FieldSpec("expiredTs", "uint64"),
}

SYNC_GROUP_MSG_LIST_SCHEMA = {
    1: FieldSpec("msgList", "message", repeated=True, message_schema=SYNC_MSG_SCHEMA),
    2: FieldSpec("forceUpdate", "bool"),
    3: FieldSpec("hasMore", "uint32"),
}

SYNC_RESPONSE_SCHEMA = {
    1: FieldSpec("uid", "string"),
    2: FieldSpec("titanid", "string"),
    3: FieldSpec("os", "uint32"),
    4: FieldSpec(
        "uidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=SYNC_GROUP_MSG_LIST_SCHEMA,
    ),
    5: FieldSpec(
        "titanidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=SYNC_GROUP_MSG_LIST_SCHEMA,
    ),
    6: FieldSpec("additionalMap", "map", key_kind="string", value_kind="string"),
    7: FieldSpec("needAck", "bool"),
    8: FieldSpec("interval", "uint32"),
}

ACK_ITEM_DETAIL_INFO_SCHEMA = {
    1: FieldSpec("bizType", "uint32"),
    2: FieldSpec("subType", "uint32"),
    3: FieldSpec("msgId", "string"),
    4: FieldSpec("timestamp", "uint64"),
}

ACK_GROUP_ITEM_SCHEMA = {
    1: FieldSpec("clientOffset", "uint64"),
    2: FieldSpec("msgMap", "map", key_kind="uint64", value_kind="uint32"),
    3: FieldSpec(
        "msgDetailMap",
        "map",
        key_kind="uint64",
        value_kind="message",
        value_schema=ACK_ITEM_DETAIL_INFO_SCHEMA,
    ),
}

ACK_SCHEMA = {
    1: FieldSpec(
        "uidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=ACK_GROUP_ITEM_SCHEMA,
    ),
    2: FieldSpec(
        "titanidMap",
        "map",
        key_kind="uint32",
        value_kind="message",
        value_schema=ACK_GROUP_ITEM_SCHEMA,
    ),
    3: FieldSpec("additionalMap", "map", key_kind="string", value_kind="string"),
}

TOKEN_FAIL_SCHEMA = {
    1: FieldSpec("errCode", "uint32"),
    2: FieldSpec("accesstoken", "bytes"),
}

NOTIFY_INNER_SCHEMA = {
    1: FieldSpec("bizType", "uint32"),
    2: FieldSpec("body", "bytes"),
}


def random_titan_id(prefix: str = "", length: int = 21) -> str:
    alphabet = "bjectSymhasOwnProp-0123456789ABCDEFGHIJKLMNQRTUVWXYZ_dfgiklquvxz"
    return f"{prefix}{int(time.time() * 1000)}_{''.join(random.choice(alphabet) for _ in range(length))}"


def encode_app_info(
    *,
    access_token: str,
    uid: str = "",
    titan_id: str = "",
    ua: str = DEFAULT_USER_AGENT,
    os: int = 5,
    custom_payload: dict[str, Any] | None = None,
    auth_type: int | None = None,
    scene_type: int | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "titanid": titan_id,
        "ua": ua,
        "os": os,
        "uid": str(uid),
        "repackage": False,
        "accesstoken": access_token,
    }
    if custom_payload:
        payload["customPayload"] = {
            str(key): _ensure_bytes(value) for key, value in custom_payload.items()
        }
    if auth_type:
        payload["authType"] = int(auth_type)
    if scene_type:
        payload["sceneType"] = int(scene_type)
    return encode_message(payload, APP_INFO_SCHEMA)


def decode_app_info(data: bytes) -> dict[str, Any]:
    return decode_message(data, APP_INFO_SCHEMA)


def encode_titan_session_request(app_info: bytes) -> bytes:
    return encode_message(
        {
            "requestType": 4,
            "protocolVersion": PROTOCOL_DEFAULT,
            "isPushConn": True,
            "encryptedAppInfo": app_info,
        },
        TITAN_SESSION_REQUEST_SCHEMA,
    )


def encode_titan_upstream(values: dict[str, Any]) -> bytes:
    return encode_message(values, TITAN_UPSTREAM_SCHEMA)


def decode_titan_downstream(data: bytes) -> dict[str, Any]:
    payload = decode_message(data, TITAN_DOWNSTREAM_SCHEMA)
    if payload.get("compress") == COMPRESS_GZIP:
        if payload.get("body"):
            payload["body"] = ungzip(payload["body"])
        if payload.get("extension"):
            payload["extension"] = ungzip(payload["extension"])
    return payload


def encode_titan_frame(
    *,
    magic: int,
    cmd: int,
    ctx: int,
    reserve: int = RESERVE_DEFAULT,
    payload: bytes | None = None,
) -> bytes:
    payload = payload or b""
    header = struct.pack("!hhiii", magic, cmd, ctx, reserve, len(payload))
    return header + payload


def decode_titan_frame(data: bytes, *, decode_downstream: bool = True) -> TitanFrame:
    data = _ensure_bytes(data)
    if len(data) < 16:
        raise ValueError("titan frame is shorter than the 16-byte header")
    magic, cmd, ctx, reserve, body_len = struct.unpack("!hhiii", data[:16])
    payload_bytes = data[16: 16 + body_len]
    if len(payload_bytes) != body_len:
        raise ValueError("titan frame payload is incomplete")
    payload = None
    if decode_downstream and payload_bytes:
        payload = decode_titan_downstream(payload_bytes)
    return TitanFrame(
        magic=magic,
        cmd=cmd,
        ctx=ctx,
        reserve=reserve,
        body_len=body_len,
        payload_bytes=payload_bytes,
        payload=payload,
    )


def build_titan_upstream_frame(
    *,
    app_id: int,
    ctx: int,
    command: str,
    protocol: int = PROTOCOL_DEFAULT,
    host: str = DEFAULT_HOST,
    body: bytes | None = None,
    appinfo: bytes | None = None,
    session_resumption_req: bytes | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "appId": int(app_id),
        "command": command,
        "protocol": int(protocol),
        "compress": COMPRESS_NONE,
        "host": host,
        "upstreamSeq": int(ctx),
    }
    if appinfo is not None:
        payload["appinfo"] = appinfo
    if session_resumption_req is not None:
        payload["sessionResumptionReq"] = session_resumption_req
    if body is not None:
        payload["body"] = body
    return encode_titan_frame(
        magic=MAGIC_UPSTREAM,
        cmd=CMD_TITAN,
        ctx=ctx,
        reserve=RESERVE_DEFAULT,
        payload=encode_titan_upstream(payload),
    )


def build_session_frame(
    *,
    app_id: int,
    access_token: str,
    uid: str,
    titan_id: str,
    ctx: int,
    host: str = DEFAULT_HOST,
    ua: str = DEFAULT_USER_AGENT,
    os: int = 5,
    custom_payload: dict[str, Any] | None = None,
    auth_type: int | None = None,
    scene_type: int | None = None,
) -> bytes:
    app_info = encode_app_info(
        access_token=access_token,
        uid=uid,
        titan_id=titan_id,
        ua=ua,
        os=os,
        custom_payload=custom_payload,
        auth_type=auth_type,
        scene_type=scene_type,
    )
    session = encode_titan_session_request(app_info)
    return build_titan_upstream_frame(
        app_id=app_id,
        ctx=ctx,
        command=SESSION_COMMAND,
        protocol=PROTOCOL_DEFAULT,
        host=host,
        session_resumption_req=session,
    )


def build_pong_frame(*, app_id: int, ctx: int, host: str = DEFAULT_HOST) -> bytes:
    return build_titan_upstream_frame(
        app_id=app_id,
        ctx=ctx,
        command=PONG_COMMAND,
        protocol=PROTOCOL_DEFAULT,
        host=host,
    )


def build_heartbeat_frame(*, ctx: int) -> bytes:
    return encode_titan_frame(
        magic=MAGIC_HEARTBEAT,
        cmd=CMD_HEARTBEAT,
        ctx=ctx,
        reserve=RESERVE_DEFAULT,
        payload=None,
    )


def build_sync_frame(
    *,
    app_id: int,
    ctx: int,
    host: str = DEFAULT_HOST,
    sync_all: bool = True,
    uid_offset_map: dict[int, int] | None = None,
    titanid_offset_map: dict[int, int] | None = None,
    accept: int | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "syncAll": sync_all,
    }
    if uid_offset_map:
        body["uidOffsetMap"] = uid_offset_map
    if titanid_offset_map:
        body["titanidOffsetMap"] = titanid_offset_map
    if accept is not None:
        body["accept"] = accept
    return build_titan_upstream_frame(
        app_id=app_id,
        ctx=ctx,
        command=SYNC_COMMAND,
        protocol=PROTOCOL_DEFAULT,
        host=host,
        body=encode_message(body, SYNC_SCHEMA),
    )


def build_notify_data_lite_ack_frame(
    *,
    app_id: int,
    ctx: int,
    msg_id: str,
    biz_type: int,
    host: str = DEFAULT_HOST,
    additional_map: dict[str, str] | None = None,
    sub_type: int | None = None,
    timestamp: int | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "msgId": msg_id,
        "bizType": biz_type,
    }
    if additional_map:
        body["additionalMap"] = additional_map
    if sub_type is not None:
        body["subType"] = sub_type
    if timestamp is not None:
        body["timestamp"] = timestamp
    return build_titan_upstream_frame(
        app_id=app_id,
        ctx=ctx,
        command=NOTIFY_DATA_LITE_ACK_COMMAND,
        protocol=PROTOCOL_DEFAULT,
        host=host,
        body=encode_message(body, NOTIFY_DATA_LITE_ACK_SCHEMA),
    )


def build_ack_frame(
    *,
    app_id: int,
    ctx: int,
    ack: dict[str, Any],
    host: str = DEFAULT_HOST,
) -> bytes:
    return build_titan_upstream_frame(
        app_id=app_id,
        ctx=ctx,
        command=ACK_COMMAND,
        protocol=PROTOCOL_DEFAULT,
        host=host,
        body=encode_message(ack, ACK_SCHEMA),
    )


def decode_extension(data: bytes | None) -> dict[str, Any]:
    if not data:
        return {}
    extension = decode_message(data, EXTENSION_MAP_SCHEMA)
    info = extension.get("info") or {}
    decoded_info: dict[str, Any] = {}
    for key, value in info.items():
        if key == "reconnection":
            decoded_info[key] = decode_message(value, RECONNECT_INFO_SCHEMA)
        else:
            decoded_info[key] = value
    extension["decodedInfo"] = decoded_info
    return extension


def decode_notify_data_lite(body: bytes) -> dict[str, Any]:
    payload = decode_message(body, NOTIFY_DATA_LITE_SCHEMA)
    if payload.get("payload"):
        payload["decodedPayload"] = decode_body_payload(payload["payload"])
    return payload


def decode_notify(body: bytes) -> dict[str, Any]:
    return decode_message(body, NOTIFY_SCHEMA)


def decode_notify_data(body: bytes) -> dict[str, Any]:
    payload = decode_message(body, NOTIFY_DATA_SCHEMA)
    for map_name in ("uidMap", "titanidMap"):
        for group in (payload.get(map_name) or {}).values():
            for msg in group.get("msgList") or []:
                if msg.get("payload"):
                    msg["decodedPayload"] = decode_body_payload(msg["payload"])
    return payload


def decode_sync_response(body: bytes) -> dict[str, Any]:
    payload = decode_message(body, SYNC_RESPONSE_SCHEMA)
    for map_name in ("uidMap", "titanidMap"):
        for group in (payload.get(map_name) or {}).values():
            for msg in group.get("msgList") or []:
                if msg.get("payload"):
                    msg["decodedPayload"] = decode_body_payload(msg["payload"])
    return payload


def decode_notify_inner(body: bytes) -> dict[str, Any]:
    payload = decode_message(body, NOTIFY_INNER_SCHEMA)
    if payload.get("bizType") == 5001 and payload.get("body"):
        token_fail = decode_message(payload["body"], TOKEN_FAIL_SCHEMA)
        if token_fail.get("accesstoken"):
            token_fail["decodedAccessToken"] = decode_body_payload(
                token_fail["accesstoken"],
                parse_json=False,
            )
        payload["tokenFail"] = token_fail
    return payload


def ungzip(data: bytes) -> bytes:
    data = _ensure_bytes(data)
    try:
        return zlib.decompress(data, zlib.MAX_WBITS | 16)
    except zlib.error:
        return zlib.decompress(data)


def decode_body_payload(data: bytes, *, parse_json: bool = True) -> Any:
    raw_bytes = _ensure_bytes(data)
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    decoded_text = raw_text
    try:
        if parse_json:
            return json.loads(_normalize_json_text(raw_text))
    except ValueError:
        pass

    try:
        decoded = base64.b64decode(raw_bytes, validate=True)
        decoded_text = decoded.decode("utf-8", errors="replace")
    except Exception:
        try:
            decoded_text = raw_bytes.decode("latin1", errors="replace")
        except Exception:
            decoded_text = raw_text

    decoded_text = _normalize_json_text(decoded_text)
    if not parse_json:
        return decoded_text
    try:
        return json.loads(decoded_text)
    except ValueError:
        return decoded_text


def _normalize_json_text(text: str) -> str:
    return (
        text.replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\u2028", "")
    )


def summarize_downstream(frame: TitanFrame) -> dict[str, Any]:
    payload = frame.payload or {}
    summary = {
        "magic": frame.magic,
        "cmd": frame.cmd,
        "ctx": frame.ctx,
        "reserve": frame.reserve,
        "bodyLen": frame.body_len,
        "payload": payload,
    }
    command = payload.get("command")
    body = payload.get("body")
    if command == NOTIFY_DATA_LITE_COMMAND and body:
        summary["decodedBody"] = decode_notify_data_lite(body)
    elif command == NOTIFY_COMMAND and body:
        summary["decodedBody"] = decode_notify(body)
    elif command == NOTIFY_DATA_COMMAND and body:
        summary["decodedBody"] = decode_notify_data(body)
    elif command == NOTIFY_INNER_COMMAND and body:
        summary["decodedBody"] = decode_notify_inner(body)
    elif command == SYNC_COMMAND and body:
        summary["decodedBody"] = decode_sync_response(body)
    if payload.get("extension"):
        summary["extension"] = decode_extension(payload["extension"])
    return summary


# Compatibility aliases for the wording commonly used while reverse-engineering
# this bundle. The wire data is protobuf/gzip framed bytes, not AES ciphertext.
encrypt_message = encode_titan_frame
decrypt_message = decode_titan_frame
