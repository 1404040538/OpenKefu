# -*- coding: utf-8 -*-
"""千牛 impaas 消息模型解析（对齐 PDD chat 的分层习惯）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


CONTENT_TYPE_TEXT = 1
CONTENT_TYPE_CUSTOM = 101


@dataclass
class ImpaasMessage:
    message_id: str
    cid: str
    sender_uid: str  # 形如 1234567890@cntaobao
    content_type: int
    text: str
    create_at: int  # ms
    raw: dict = field(default_factory=dict)

    @property
    def sender_uid_num(self) -> str:
        return self.sender_uid.split("@", 1)[0]


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
    return ImpaasMessage(
        message_id=message_id,
        cid=str(msg.get("cid") or ""),
        sender_uid=sender_uid,
        content_type=int(msg.get("content", {}).get("contentType") or 0),
        text=_extract_text(msg.get("content") or {}),
        create_at=int(msg.get("createAt") or 0),
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
