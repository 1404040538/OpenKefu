# -*- coding: utf-8 -*-
"""千牛 impaas 消息模型解析（对齐 PDD chat 的分层习惯）。"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any


CONTENT_TYPE_TEXT = 1
CONTENT_TYPE_IMAGE = 2
CONTENT_TYPE_CUSTOM = 101

# custom.type 子类型（chat-core bundle 逆向 2026-10-08）
CUSTOM_TYPE_DYNAMIC = 2
CUSTOM_TYPE_TEMPLATE = 1
CUSTOM_TYPE_YUNPAN = 3
CUSTOM_TYPE_ITEM = 4
CUSTOM_TYPE_IMAGE = 7
CUSTOM_TYPE_AUDIO = 8
CUSTOM_TYPE_VIDEO = 9
CUSTOM_TYPE_EMOTION = 11
CUSTOM_TYPE_SYSTEM = 1001

# ddmedia 域名（authType 0/6 走 CDN，其余走下载源站）
DDMEDIA_CDN_HOST = "down-cdn.dingtalk.com"
DDMEDIA_HOST = "down.dingtalk.com"
OLD_MEDIA_HOST = "static.dingtalk.com"


@dataclass
class ImpaasMessage:
    message_id: str
    cid: str
    sender_uid: str  # 形如 1234567890@cntaobao
    content_type: int
    text: str
    create_at: int  # ms
    kind: str = "text"  # text / image / card
    image_url: str = ""  # kind=image 时可直接访问的图片 URL
    image_info: dict = field(default_factory=dict)  # 宽高等元数据
    raw: dict = field(default_factory=dict)

    @property
    def sender_uid_num(self) -> str:
        return self.sender_uid.split("@", 1)[0]


def _b64_decode_bytes(text: str) -> bytes:
    """base64url/base64 混合安全解码（chat-core 的 Nt()+decode）。"""
    normalized = text.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * (-len(normalized) % 4)
    return base64.b64decode(padded)


def _b64_encode_utf8(text: str) -> str:
    """UTF-8 安全 base64（chat-core 的 ae()=j()，对应发送侧 custom.data 编码）。"""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def media_id_to_url(media_id: str, *, thumb: bool = False) -> str:
    """impaas mediaId → 可访问 URL（chat-core It() 逻辑）。

    ``$`` 前缀 = ddmedia（新版），``@`` 前缀 = 老版 media；
    其他输入视为已是 URL 原样返回。
    ddmedia 的 base64url 体按字节索引解出 AUTH_TYPE/TYPE/WIDTH/HEIGHT，
    AUTH_TYPE 0/6 走 CDN 域名。缩略图通过阿里 OSS 切图后缀实现。
    """
    media_id = str(media_id or "")
    if not media_id or media_id[0] not in ("$", "@"):
        return re.sub(r"^http(?!s)", "https", media_id)
    is_ddmedia = media_id[0] == "$"
    body = media_id[1:].replace("-", "+").replace("_", "/")
    try:
        raw = _b64_decode_bytes(body)
        info = [b for b in raw]
    except Exception:
        info = []
    if is_ddmedia:
        # St 索引：IDC=1 TYPE=2 AUTH_TYPE=3 WIDTH=4 HEIGHT=5
        auth_type = info[3] if len(info) > 3 else 0
        ext = "jpg"
        if len(info) > 2 and 0 < info[2] < 32:
            ext = {1: "webp", 2: "png", 3: "jpg", 4: "gif"}.get(info[2], "jpg")
        host = DDMEDIA_CDN_HOST if auth_type in (0, 6) else DDMEDIA_HOST
        suffix = "_120x120q90.jpg" if thumb else "_620x10000q90.jpg"
        path = f"{body}.{ext}{suffix}"
    else:
        # 老版 Et 索引：TYPE=0 SIZE=1 HEIGHT=2 WIDTH=3
        ext = "jpg"
        if info and 0 < info[0] < 32:
            ext = {1: "webp", 2: "png", 3: "jpg", 4: "gif"}.get(info[0], "jpg")
        width = info[3] if len(info) > 3 else 0
        height = info[2] if len(info) > 2 else 0
        host = OLD_MEDIA_HOST
        path = f"{body}_{width}_{height}.{ext}"
    return f"https://{host}/{'ddmedia' if is_ddmedia else 'media'}/{path}"


def _parse_image_content(content: dict) -> tuple[str, dict]:
    """解出图片 URL 与元数据，兼容两种编码。

    - contentType=2：photo.mediaId（$ddmedia / @老media）
    - contentType=101 + custom.type=7：custom.data = base64(JSON{url,...})
    """
    photo = content.get("photo")
    if isinstance(photo, dict) and photo.get("mediaId"):
        meta = {
            "mediaId": str(photo.get("mediaId")),
            "picSize": photo.get("picSize"),
            "fileType": photo.get("fileType"),
            "width": photo.get("width"),
            "height": photo.get("height"),
        }
        return media_id_to_url(str(photo["mediaId"])), meta
    custom = content.get("custom")
    if isinstance(custom, dict):
        data = custom.get("data") or ""
        try:
            decoded = json.loads(_b64_decode_bytes(str(data)).decode("utf-8")) \
                if not str(data).startswith("{") else json.loads(str(data))
        except Exception:
            decoded = {}
        if isinstance(decoded, dict) and decoded.get("url"):
            meta = {
                "fileId": decoded.get("fileId"),
                "size": decoded.get("size"),
                "width": decoded.get("width"),
                "height": decoded.get("height"),
                "isOriginal": decoded.get("isOriginal"),
                "suffix": decoded.get("suffix"),
            }
            return str(decoded["url"]), meta
    return "", {}


def encode_custom_image(image_meta: dict) -> dict:
    """构造发送侧 custom IMAGE content（对齐 chat-core Al()：base64(JSON)）。"""
    payload = {
        "fileId": image_meta.get("fileId", ""),
        "size": int(image_meta.get("size") or 0),
        "url": image_meta.get("url", ""),
        "width": int(image_meta.get("width") or 0),
        "height": int(image_meta.get("height") or 0),
        "isOriginal": 1,
        "suffix": image_meta.get("suffix", "jpg"),
    }
    return {"contentType": CONTENT_TYPE_CUSTOM,
            "custom": {"type": CUSTOM_TYPE_IMAGE, "data": _b64_encode_utf8(json.dumps(payload, separators=(",", ":")))}}


@dataclass
class ImpaasConversation:
    cid: str
    biz_type: str
    modify_time: int
    last_message: ImpaasMessage | None = None
    account_role_map: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def peer_uid_num(self, my_uid_num: str) -> str | None:
        """从 cid 中解析对方 uid：cid = <a>.1-<b>.1#bizType@domain。"""
        head = self.cid.split("#", 1)[0]
        pair = head.split("-", 1)
        if len(pair) != 2:
            return None
        a = pair[0].rsplit(".", 1)[0]
        b = pair[1].rsplit(".", 1)[0]
        if a == my_uid_num:
            return b
        if b == my_uid_num:
            return a
        return b or a


def _extract_text(content: dict) -> str:
    if not isinstance(content, dict):
        return ""
    text = content.get("text")
    if isinstance(text, dict):
        return str(text.get("content") or "")
    custom = content.get("custom")
    if isinstance(custom, dict):
        return str(custom.get("summary") or custom.get("title") or "")
    return ""


def parse_message(model: dict) -> ImpaasMessage | None:
    """从 listUserMessages 的 userMessageModel 解析消息。"""
    if not isinstance(model, dict):
        return None
    msg = model.get("message") or model
    message_id = str(msg.get("messageId") or msg.get("uuid") or "")
    if not message_id:
        return None
    sender = msg.get("sender") or {}
    sender_uid = sender.get("uid", "") if isinstance(sender, dict) else str(sender)
    content = msg.get("content") or {}
    content_type = int(content.get("contentType") or 0) if isinstance(content, dict) else 0
    kind = "text"
    image_url = ""
    image_info: dict = {}
    if content_type == CONTENT_TYPE_IMAGE:
        image_url, image_info = _parse_image_content(content)
        kind = "image" if image_url else "card"
    elif content_type == CONTENT_TYPE_CUSTOM:
        custom_type = 0
        custom = content.get("custom")
        if isinstance(custom, dict):
            try:
                custom_type = int(custom.get("type") or 0)
            except (TypeError, ValueError):
                custom_type = 0
        if custom_type == CUSTOM_TYPE_IMAGE:
            image_url, image_info = _parse_image_content(content)
            kind = "image" if image_url else "card"
        else:
            kind = "card"
    text = _extract_text(content)
    if kind == "image":
        text = text or "[图片]"
    return ImpaasMessage(
        message_id=message_id,
        cid=str(msg.get("cid") or ""),
        sender_uid=sender_uid,
        content_type=content_type,
        text=text,
        create_at=int(msg.get("createAt") or 0),
        kind=kind,
        image_url=image_url,
        image_info=image_info,
        raw=model,
    )


def parse_conversation(uc: dict) -> ImpaasConversation | None:
    """从 listNewestPagination 的 userConv 解析会话。"""
    if not isinstance(uc, dict):
        return None
    sc = (uc.get("singleChatUserConversation") or {})
    conv = sc.get("singleChatConversation") or {}
    cid = str(conv.get("cid") or "")
    if not cid:
        return None
    last = None
    last_raw = sc.get("lastMessage") or {}
    msg_raw = last_raw.get("message")
    if msg_raw:
        last = parse_message({"message": msg_raw})
    role_map = {}
    role_str = conv.get("extension", {}).get("accountRoleMap", "")
    if isinstance(role_str, str):
        # 形如 {1234567890:"seller",9876543210:"buyer"} 的 JS 对象
        for m in re.finditer(r'(\d+):\s*"(seller|buyer)"', role_str):
            role_map[m.group(1)] = m.group(2)
    return ImpaasConversation(
        cid=cid,
        biz_type=str(conv.get("bizType") or ""),
        modify_time=int(sc.get("modifyTime") or 0),
        last_message=last,
        account_role_map=role_map,
        raw=sc,
    )
