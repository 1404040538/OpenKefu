from __future__ import annotations

import re


GREETING_PATTERNS = re.compile(
    r"^(你好|您好|在吗|在不在|在么|嗨|hi|hello|哈喽|hallo|早上好|下午好|晚上好|"
    r"有人在吗|客服在吗|你好在吗|您好客服|你好亲|您好亲|亲在吗|"
    r"新年好|节日快乐|"
    r"[!！？?。,，.\s]*)+$",
    re.IGNORECASE,
)

THANKS_PATTERNS = re.compile(
    r"^(谢谢|感谢|多谢|谢了|好的谢谢|谢谢啊|感谢你|谢谢你|辛苦了|"
    r"好的|ok|OK|收到|明白了|知道了|懂了|那行|那好吧|"
    r"[!！？?。,，.\s]*)+$",
    re.IGNORECASE,
)

GREETING_REPLIES = [
    "您好，欢迎光临！请问有什么可以帮您的？",
    "亲，您好～有什么需要帮您看看的吗？",
    "您好！我是店铺客服，有什么问题随时问我哦。",
]

THANKS_REPLIES = [
    "不客气哦，有什么需要再找我～",
    "应该的，亲～还有其他问题吗？",
    "不用谢，很高兴能帮到您！",
]


FAST_FAQ: dict[str, list[str]] = {
    "发货时间": [
        "亲，一般付款后48小时内会为您安排发货的，具体以订单页面显示为准哦。",
        "您好，下单后我们会在48小时内发出，快递揽收后就能看到物流信息了。",
    ],
    "退换货": [
        "亲，支持7天无理由退换货的，您可以在订单页面申请售后，我来帮您跟进处理。",
    ],
    "包邮": [
        "亲，店铺大部分商品都是包邮的哦，具体可以看商品详情页说明。",
    ],
}


def detect_fast_reply(content: str) -> str | None:
    if not content:
        return None
    content = content.strip()

    if GREETING_PATTERNS.match(content):
        return _pick(GREETING_REPLIES)

    if THANKS_PATTERNS.match(content):
        return _pick(THANKS_REPLIES)

    for keyword, replies in FAST_FAQ.items():
        if keyword in content and len(content) <= 30:
            return _pick(replies)

    return None


_fast_index: int = 0


def _pick(options: list[str]) -> str:
    global _fast_index
    result = options[_fast_index % len(options)]
    _fast_index += 1
    return result
