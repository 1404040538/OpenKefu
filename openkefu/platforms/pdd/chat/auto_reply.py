import ast
import json
import logging
import random


logger = logging.getLogger(__name__)


DEFAULT_REPLY_TEXTS = [
    "您好，请问有什么可以帮您？",
    "收到，我这边帮您看一下。",
    "您好，稍等，我马上为您确认。",
    "好的，您发的信息我已收到。",
    "您好，请稍等一下哦。",
]

SYSTEM_TEMPLATE_NAMES = {
    "remind_help_order",
}


def parse_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        data = bytes(payload)
    elif isinstance(payload, str):
        value = payload.strip()
        if value.startswith(("b'", 'b"')):
            try:
                literal = ast.literal_eval(value)
                data = bytes(literal) if isinstance(literal, (bytes, bytearray)) else value.encode("utf-8")
            except (SyntaxError, ValueError):
                data = value.encode("utf-8")
        else:
            data = value.encode("utf-8")
    else:
        return None

    for encoding in ("utf-8", "latin1"):
        try:
            return json.loads(data.decode(encoding, errors="replace"))
        except ValueError:
            continue
    return None


def messages_from_payload(payload):
    payload = parse_payload(payload)
    if not isinstance(payload, dict):
        return []

    messages = []
    push_data = payload.get("push_data")
    if isinstance(push_data, dict):
        for item in push_data.get("data") or []:
            item_message = item.get("message") if isinstance(item, dict) else None
            if isinstance(item_message, dict):
                messages.append(with_reply_context(item_message, item))

    item_message = payload.get("message")
    if isinstance(item_message, dict):
        messages.append(item_message)
    return messages


def incoming_user_messages(message):
    return [
        item
        for item in incoming_chat_messages(message)
        if _is_user_message(item)
    ]


def incoming_display_messages(message):
    return [
        item
        for item in incoming_chat_messages(message)
        if _is_user_message(item) or is_display_system_notice_message(item)
    ]


def incoming_chat_messages(message):
    if not isinstance(message, dict) or message.get("type") != "event":
        return []
    event = message.get("event") or {}
    if not isinstance(event, dict) or event.get("type") != "message":
        return []

    chat_messages = []
    for item in event.get("messages") or []:
        item_message = item.get("message") if isinstance(item, dict) else None
        if isinstance(item_message, dict):
            chat_messages.append(with_reply_context(item_message, item))

    if not chat_messages:
        decoded = event.get("decoded") if isinstance(event.get("decoded"), dict) else {}
        frame = event.get("frame") if isinstance(event.get("frame"), dict) else {}
        decoded_body = frame.get("decodedBody") if isinstance(frame.get("decodedBody"), dict) else {}
        for payload in (
            event.get("payload"),
            decoded.get("decodedPayload"),
            decoded.get("payload"),
            decoded_body.get("decodedPayload"),
            decoded_body.get("payload"),
        ):
            for item_message in messages_from_payload(payload):
                if isinstance(item_message, dict):
                    chat_messages.append(item_message)
    return chat_messages


def with_reply_context(message, wrapper=None):
    message = dict(message)
    wrapper = wrapper if isinstance(wrapper, dict) else {}
    context = dict(message.get("_reply_context") or {})
    for key in ("chat_type_id", "chat_type", "conv_id", "client_msg_id", "seq_type", "seq_id"):
        value = wrapper.get(key, message.get(key))
        if value not in (None, ""):
            context[key] = value
    if context:
        message["_reply_context"] = context
    return message


def _is_user_message(message):
    if not isinstance(message, dict):
        return False
    if is_system_notice_message(message):
        return False
    sender = message.get("from") or {}
    uid = sender.get("uid") if sender.get("role") == "user" else None
    return uid not in (None, "")


def is_system_notice_message(message):
    if not isinstance(message, dict):
        return False
    sender = message.get("from") or {}
    if sender.get("role") == "system":
        return True
    if message.get("template_name") in SYSTEM_TEMPLATE_NAMES:
        return True
    if message.get("no_unreply_hint"):
        return True
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    if message.get("type") == 31 and info.get("mall_item_content") and info.get("mall_content"):
        return True
    return False


def is_display_system_notice_message(message):
    if not isinstance(message, dict):
        return False
    if message.get("template_name") in SYSTEM_TEMPLATE_NAMES:
        return True
    if message.get("no_unreply_hint") and (message.get("content") or message.get("decoded_content")):
        return True
    return False


def message_dedupe_key(message):
    sender = message.get("from") or {}
    uid = str(sender.get("uid") or "")
    msg_id = message.get("msg_id")
    if msg_id not in (None, ""):
        return ("msg_id", str(msg_id))
    client_msg_id = message.get("client_msg_id")
    if client_msg_id not in (None, ""):
        return ("client_msg_id", str(client_msg_id))
    return (
        "content",
        uid,
        str(message.get("content") or ""),
        str(message.get("ts") or ""),
        str(message.get("type") or ""),
    )


def message_kind(message):
    if is_system_notice_message(message):
        return "system"
    msg_type = message.get("type")
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    content = str(message.get("decoded_content") or message.get("content") or "")
    if msg_type == 1:
        return "image"
    if info.get("goodsID") or info.get("goodsName") or info.get("linkUrl"):
        return "goods"
    if content.startswith(("http://", "https://")):
        return "link"
    return "text"


def _sender_nickname(message, sender, info):
    for source in (message, sender, info):
        if not isinstance(source, dict):
            continue
        for key in (
            "nickname",
            "nick_name",
            "nickName",
            "name",
            "username",
            "user_name",
            "display_name",
        ):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def user_message_summary(message):
    sender = message.get("from") or {}
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    content = message.get("decoded_content") or message.get("content") or ""
    if is_system_notice_message(message) and not content:
        content = (
            info.get("mall_content")
            or info.get("mall_item_content")
            or info.get("content")
            or message.get("template_name")
            or ""
        )
    summary = {
        "uid": str(sender.get("uid") or ""),
        "nickname": _sender_nickname(message, sender, info),
        "msg_id": str(message.get("msg_id") or ""),
        "type": message.get("type"),
        "kind": message_kind(message),
        "content": content,
    }
    if info.get("goodsID") or info.get("goodsName") or info.get("linkUrl"):
        summary["goods"] = {
            "id": str(info.get("goodsID") or ""),
            "name": info.get("goodsName") or "",
            "price": info.get("goodsPrice") or "",
            "link": info.get("linkUrl") or summary["content"],
        }
    if isinstance(message.get("size"), dict):
        summary["size"] = message["size"]
    return summary


class AutoReplyHandler:
    def __init__(self, customer_service, *, token_result, mall_id, reply_texts=None):
        self.customer_service = customer_service
        self.token_result = token_result
        self.mall_id = mall_id
        self.reply_texts = list(reply_texts or DEFAULT_REPLY_TEXTS)
        self.replied_message_keys = set()

    def handle(self, event):
        for user_message in incoming_user_messages(event):
            dedupe_key = message_dedupe_key(user_message)
            if dedupe_key in self.replied_message_keys:
                continue
            self.replied_message_keys.add(dedupe_key)
            self.print_user_message(user_message)
            self.reply(user_message)

    def print_user_message(self, user_message):
        logger.info(
            "user message %s",
            json.dumps(user_message_summary(user_message), ensure_ascii=False, default=str),
        )

    def reply(self, user_message):
        user_uid = str((user_message.get("from") or {}).get("uid"))
        context = user_message.get("_reply_context") if isinstance(user_message.get("_reply_context"), dict) else {}
        try:
            result = self.customer_service.send_text_message(
                user_uid,
                random.choice(self.reply_texts),
                token_result=self.token_result,
                mall_id=self.mall_id,
                conv_id=context.get("conv_id"),
                chat_type_id=context.get("chat_type_id"),
                chat_type=context.get("chat_type"),
            )
            logger.info(
                "auto reply sent result_type=%s success=%s",
                type(result).__name__,
                result.get("success") if isinstance(result, dict) else None,
            )
        except Exception as exc:
            logger.exception("auto reply failed: %s", exc)
