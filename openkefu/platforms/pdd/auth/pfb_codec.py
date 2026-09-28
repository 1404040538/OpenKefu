import base64
import hashlib
import json
from pathlib import Path
import zlib


DEFAULT_SIGN_KEY = "feSJ293j0sIj9u3lkj"
DEFAULT_APP_KEY = "fe"
CUSTOM_PAIR_MARKER = b"\xe2\x03"


def _read_varint(data: bytes, offset: int):
    """读取 protobuf 风格的 varint，返回 (value, new_offset)。"""
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("varint data is incomplete")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint is too long")


def _write_varint(value: int):
    """使用 protobuf 风格的 varint 编码整数。"""
    if value < 0:
        raise ValueError("varint only supports non-negative integers")
    output = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            output.append(to_write | 0x80)
        else:
            output.append(to_write)
            return bytes(output)


def _parse_custom_pairs(data: bytes):
    """
    解析当前 pfb data 样本使用的自定义键值流。

    记录结构：
    - 固定标记：e2 03
    - varint 长度：utf-8 字节长度 * 2
    - utf-8 载荷
    """
    items = []
    offset = 0
    while offset < len(data):
        if data[offset: offset + 2] != CUSTOM_PAIR_MARKER:
            raise ValueError(f"invalid marker at offset={offset}, bytes={data[offset:offset + 8]!r}")
        offset += 2
        encoded_length, offset = _read_varint(data, offset)
        if encoded_length % 2 != 0:
            raise ValueError(f"encoded length must be even, offset={offset}, length={encoded_length}")
        byte_length = encoded_length // 2
        chunk = data[offset: offset + byte_length]
        if len(chunk) != byte_length:
            raise ValueError("custom pair payload is incomplete")
        offset += byte_length
        items.append(chunk.decode("utf-8"))

    if len(items) % 2 != 0:
        return {"items": items}
    return {items[i]: items[i + 1] for i in range(0, len(items), 2)}


def _encode_custom_pairs(payload: dict):
    """将字典重新编码为自定义键值流。"""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    output = bytearray()
    for key, value in payload.items():
        for text in (key, value):
            encoded = str(text).encode("utf-8")
            output.extend(CUSTOM_PAIR_MARKER)
            output.extend(_write_varint(len(encoded) * 2))
            output.extend(encoded)
    return bytes(output)


def _b64url_decode(text: str):
    text = text.replace("-", "+").replace("_", "/")
    text += "=" * ((-len(text)) % 4)
    return base64.b64decode(text)


def _b64url_encode(data: bytes):
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")


def maybe_expand_json_values(payload: dict):
    """尝试解析看起来像 JSON 的字符串值。"""
    expanded = {}
    for key, value in payload.items():
        if isinstance(value, str) and value[:1] in ("{", "["):
            try:
                expanded[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        expanded[key] = value
    return expanded


def flatten_json_values(payload: dict):
    """将 dict/list 值压缩序列化回 JSON 字符串。"""
    flattened = {}
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            flattened[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            flattened[key] = str(value)
    return flattened


def decode_pfb_data(data: str, expand_json=False):
    """解码加密的 pfb data 字段。"""
    if not data:
        raise ValueError("data cannot be empty")

    if data.startswith("0a"):
        data = data[2:]

    compressed = _b64url_decode(data)
    decompressed = zlib.decompress(compressed)

    try:
        decoded = json.loads(decompressed.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = _parse_custom_pairs(decompressed)

    return maybe_expand_json_values(decoded) if expand_json else decoded


def encode_pfb_data(payload: dict, *, output_format: str = "custom"):
    """将载荷重新编码为加密的 pfb data 字段。"""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    if output_format == "json":
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif output_format == "custom":
        raw = _encode_custom_pairs(flatten_json_values(payload))
    else:
        raise ValueError("output_format must be 'custom' or 'json'")

    compressed = zlib.compress(raw)
    return f"0a{_b64url_encode(compressed)}"


def generate_pfb_sign(data: str, timestamp: int, sign_key: str = DEFAULT_SIGN_KEY):
    """生成 sign = sha1(signKey + timestamp + data)。"""
    # SHA-1 is mandated by the upstream PFB wire protocol; it is not used for
    # password storage, certificate validation, or any local security decision.
    return hashlib.sha1(
        f"{sign_key}{timestamp}{data}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def verify_pfb_sign(data: str, timestamp: int, sign: str, sign_key: str = DEFAULT_SIGN_KEY):
    """校验 sign 值。"""
    expected = generate_pfb_sign(data, timestamp, sign_key=sign_key)
    return expected == sign, expected


def update_encrypted_payload(
    encrypted_data: str,
    *,
    updates: dict | None = None,
    raw_data_updates: dict | None = None,
    timestamp: int | None = None,
    sign_key: str = DEFAULT_SIGN_KEY,
):
    """
    解码、更新、重新编码，并重新生成 sign。

    - updates：修改 uid / reportTimestamp / uuid1 等顶层字段
    - raw_data_updates：修改 rawData 内部的嵌套字段
    """
    payload = decode_pfb_data(encrypted_data, expand_json=True)

    if updates:
        payload.update(updates)

    if raw_data_updates:
        raw_data = payload.get("rawData")
        if not isinstance(raw_data, dict):
            raise ValueError("rawData is not a decoded JSON object")
        raw_data.update(raw_data_updates)
        payload["rawData"] = raw_data

    if timestamp is not None:
        payload["reportTimestamp"] = str(timestamp)

    encoded_data = encode_pfb_data(payload, output_format="custom")
    final_timestamp = int(timestamp if timestamp is not None else payload.get("reportTimestamp"))
    sign = generate_pfb_sign(encoded_data, final_timestamp, sign_key=sign_key)

    return {
        "payload": payload,
        "data": encoded_data,
        "timestamp": final_timestamp,
        "sign": sign,
    }


def load_template(file_path):
    """从 json 文件加载加密模板数据。"""
    template = json.loads(Path(file_path).read_text(encoding="utf-8"))
    required = {"encrypted_data", "timestamp", "sign"}
    missing = required - set(template)
    if missing:
        raise ValueError(f"template is missing required fields: {sorted(missing)}")
    return template


def build_pfb_body_from_template(
    template_data: str,
    *,
    updates: dict | None = None,
    raw_data_updates: dict | None = None,
    timestamp: int | None = None,
    sign_key: str = DEFAULT_SIGN_KEY,
    app_key: str = DEFAULT_APP_KEY,
):
    """基于加密模板数据构造最终的 pfb/a2 请求体。"""
    updated = update_encrypted_payload(
        template_data,
        updates=updates,
        raw_data_updates=raw_data_updates,
        timestamp=timestamp,
        sign_key=sign_key,
    )
    return {
        "data": updated["data"],
        "timestamp": updated["timestamp"],
        "appKey": app_key,
        "sign": updated["sign"],
        "payload": updated["payload"],
    }
