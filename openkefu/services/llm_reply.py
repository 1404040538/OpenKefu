from __future__ import annotations

import json
import re
from typing import Any

from openkefu.services.intent import build_intent_messages, normalize_intent_result
from openkefu.services.llm import LLMClient, get_llm_client
from openkefu.web.config import LLMConfig

FALLBACK_REPLY = "亲，这个问题我先帮您记录下来，会尽快安排人工客服继续处理，请您稍等一下。"


def analyze_customer_intent(
    history: list[dict[str, str]],
    *,
    knowledge_hits: list[dict[str, Any]] | None = None,
    shop_notes: list[str] | None = None,
    business_context: str | None = None,
    model: str | None = None,
    llm_config: LLMConfig | None = None,
    llm_client: LLMClient | None = None,
) -> dict[str, Any]:
    normalized_history = _normalize_history(history)
    messages = build_intent_messages(
        normalized_history,
        knowledge_hits or [],
        shop_notes,
        business_context=business_context,
    )
    if len(messages) == 1:
        raise ValueError("意图识别缺少对话内容")

    client = llm_client or _resolve_client(llm_config)

    try:
        content = client.chat(
            messages,
            model=model or client.model,
            response_format="json_object",
            max_tokens=1024,
            max_retries=1,
        )
        data = json.loads(content)
        result = normalize_intent_result(data)
        return _repair_empty_knowledge_reply(
            result,
            normalized_history,
            has_knowledge_hits=bool(knowledge_hits),
            has_business_context=bool(business_context),
        )
    except Exception as exc:
        error_msg = str(exc)

    return normalize_intent_result(
        {
            "reply": FALLBACK_REPLY,
            "intent_code": "unknown",
            "confidence": 0.2,
            "resolution_status": "need_human",
            "slots": {},
            "actions": [
                {
                    "type": "transfer_to_human",
                    "payload": {"reason": "intent_analysis_failed", "error": error_msg},
                }
            ],
            "error": error_msg,
        }
    )


def _repair_empty_knowledge_reply(
    intent: dict[str, Any],
    history: list[dict[str, str]],
    *,
    has_knowledge_hits: bool,
    has_business_context: bool,
) -> dict[str, Any]:
    if has_knowledge_hits or has_business_context:
        return intent
    reply = str(intent.get("reply") or "")
    if not _reply_exposes_knowledge_gap(reply):
        return intent

    user_content = _latest_user_content(history)
    if not _looks_like_basic_shipping_consultation(user_content):
        return intent

    repaired = dict(intent)
    repaired["reply"] = "亲，一般会根据仓库、商品和收货地址匹配快递公司，具体快递以发货后的物流信息为准，您下单后可以在订单物流里查看。"
    repaired["intent_code"] = "custom"
    repaired["raw_intent_code"] = repaired.get("raw_intent_code") or intent.get("intent_code") or "custom"
    repaired["confidence"] = max(float(repaired.get("confidence") or 0), 0.8)
    repaired["resolution_status"] = "resolved"
    repaired["actions"] = [
        item
        for item in repaired.get("actions") or []
        if not (
            isinstance(item, dict)
            and item.get("type") in {"express_exception_followup", "order_remark", "return_exchange", "shipping_change", "transfer_to_human"}
        )
    ]
    raw = repaired.get("raw") if isinstance(repaired.get("raw"), dict) else {}
    repaired["raw"] = {**raw, "empty_knowledge_reply_repaired": True}
    return repaired


def _reply_exposes_knowledge_gap(reply: str) -> bool:
    text = str(reply or "")
    return bool(
        re.search(
            r"(知识库|常见问题|FAQ|faq).{0,12}(未配置|没有配置|未命中|没有命中|未查询到|没有查询到|暂无|缺少)",
            text,
        )
        or re.search(
            r"(未配置|没有配置|未命中|没有命中|未查询到|没有查询到|暂无|缺少).{0,12}(知识库|常见问题|FAQ|faq)",
            text,
        )
    )


def _latest_user_content(history: list[dict[str, str]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def _looks_like_basic_shipping_consultation(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    operation_keywords = ("拦截", "截回", "召回", "不要发货", "别发货", "停止发货", "退货", "退款", "改地址", "修改地址", "订单备注", "备注订单", "帮我备注")
    if any(keyword in normalized for keyword in operation_keywords):
        return False
    consultation_keywords = (
        "发什么快递",
        "发啥快递",
        "什么快递",
        "哪个快递",
        "哪家快递",
        "用什么快递",
        "用哪家快递",
        "发哪家",
        "什么物流",
        "哪个物流",
        "快递公司",
        "什么时候发货",
        "多久发货",
        "发货了吗",
        "发货没",
        "几天发货",
    )
    if any(keyword in normalized for keyword in consultation_keywords):
        return True
    return any(marker in normalized for marker in ("吗", "么", "嘛", "?", "？")) and any(
        keyword in normalized for keyword in ("快递", "物流", "发货")
    )


def _resolve_client(llm_config: LLMConfig | None) -> LLMClient:
    if llm_config is None:
        raise RuntimeError("缺少 LLMConfig 配置")
    return get_llm_client(llm_config)


def _normalize_history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in history:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized
