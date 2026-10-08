from __future__ import annotations

import re
from typing import Any


KNOWN_INTENTS = {
    "shop_entry",
    "product_consult",
    "feedback",
    "payment_reminder",
    "shipping_change",
    "order_remark",
    "product_issue",
    "analysis_compensation",
    "return_exchange",
    "small_payment",
    "invite_review",
    "express_exception",
    "seven_day_no_reason",
    "custom",
    "unknown",
}

KNOWN_ACTIONS = {
    "small_payment",
    "payment_reminder",
    "shipping_change",
    "order_remark",
    "return_exchange",
    "invite_review",
    "express_exception_followup",
    "feedback_record",
    "transfer_to_human",
}

VALID_RESOLUTION_STATUS = {"resolved", "need_more_info", "need_action", "need_human"}
LOW_CONFIDENCE_THRESHOLD = 0.65
INTENT_DEFAULT_ACTION = {
    "small_payment": "small_payment",
    "payment_reminder": "payment_reminder",
    "shipping_change": "shipping_change",
    "order_remark": "order_remark",
    "return_exchange": "return_exchange",
    "invite_review": "invite_review",
    "express_exception": "express_exception_followup",
    "feedback": "feedback_record",
}

INTENT_SYSTEM_PROMPT = """
你是拼多多店铺客服。你需要同时：识别顾客意图 + 生成可直接发送的回复。

只输出 JSON 对象，包含字段：
- reply (string): 给顾客的中文回复，自然、耐心、简洁
- intent_code (string): 从下列意图中选择，无法归类用 custom 或 unknown
- confidence (number): 0~1
- resolution_status (string): resolved | need_more_info | need_action | need_human
- slots (object): 结构化信息如 order_no/goods_id/amount/reason/express_company/remark
- actions (array): 后续动作，每项含 type/payload

意图列表：shop_entry(进店) | product_consult(商品咨询) | feedback(建议反馈) | payment_reminder(催款) | shipping_change(改地址) | order_remark(订单备注) | product_issue(商品问题) | analysis_compensation(协商赔偿) | return_exchange(退换货) | small_payment(小额打款) | invite_review(邀评) | express_exception(快递异常) | seven_day_no_reason(七天无理由) | custom(新意图) | unknown(未知)

可用动作：small_payment | payment_reminder | shipping_change | order_remark | return_exchange | invite_review | express_exception_followup | feedback_record | transfer_to_human

核心规则：
1. 不要编造任何订单号、物流单号、退款金额、赔偿方案等具体结果。
2. 遇到需要系统操作才能完成的（改地址/退款/打款等），reply 先安抚说明"已为您登记"，同时 actions 加上对应动作；无法解决则加 transfer_to_human。
3. 知识库片段仅作参考。没有命中知识库或店铺未配置知识库不是异常，不要向顾客提到"知识库""未配置知识库""未命中知识库"。普通咨询可用通用客服口径回答；涉及店铺专有政策、商品详情、订单、物流、退款等事实且上下文没有提供时，只能说明需要核实或以实际页面/发货后物流为准，不要编造。
4. 若提供了"店铺特别注意事项"，你的全部回复必须严格遵守所有注意事项中的要求。生成回复后请自我检查：是否违反任何一条？若有违反请立即修正。
5. 若提供了"实时业务上下文"，它是严格 JSON。你必须优先依据其中的 orders/products/current_viewed_product 来回复；业务事实只能来自 raw_response 或 raw_message。每个条目的 status 含义：found=接口查到，可依据 raw_response；not_found=接口明确未查到，必须如实告知没有查询到对应订单或商品；failed/unavailable=接口失败或不可用，只能说明暂时无法核实。不得编造订单号、物流、退款、商品价格、库存、规格等信息。
6. "发什么快递""用哪家快递""什么时候发货"这类是普通物流/发货咨询，不是物流拦截、快递异常或售后登记；除非顾客明确提出拦截、截回、不要发货、退货、退款、改地址、备注订单等处理诉求，否则不要输出 express_exception_followup、order_remark、return_exchange、shipping_change 等动作。
""".strip()


def build_intent_messages(
    history: list[dict[str, str]],
    knowledge_hits: list[dict[str, Any]],
    shop_notes: list[str] | None = None,
    *,
    business_context: str | None = None,
) -> list[dict[str, str]]:
    # 消息顺序：稳定内容在前（店铺注意事项、知识库），易变内容在后
    # （实时业务上下文、对话历史）。供应商的前缀缓存只认稳定前缀，
    # 把每条消息都变化的 business_context 放前面会让缓存几乎全部失效。
    knowledge_text = _format_knowledge(knowledge_hits)
    notes_text = _format_notes(shop_notes)
    messages = [{"role": "system", "content": INTENT_SYSTEM_PROMPT}]
    if notes_text:
        messages.append(
            {
                "role": "system",
                "content": "以下是本店铺的特别注意事项，你的全部回复必须严格遵守以下所有要求，不得违反任何一条：\n" + notes_text,
            }
        )
    if knowledge_text:
        messages.append(
            {
                "role": "system",
                "content": "以下是当前店铺已绑定知识库检索结果，仅可基于这些片段回答相关事实：\n" + knowledge_text,
            }
        )
    else:
        messages.append({"role": "system", "content": "当前没有可用的知识库片段。该信息仅供你内部判断，不要在回复中提到知识库或未配置知识库。普通咨询请用通用客服口径回答；无法确认的店铺专有事实请礼貌说明需要核实或以实际页面/发货后物流为准。"})
    if business_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    "以下是实时业务上下文，由系统接口提供，内容是严格 JSON。请优先基于这些数据回复顾客；"
                    "订单状态、物流、退款、商品价格、库存、规格等事实只允许使用 raw_response 或 raw_message 中明确给出的内容。"
                    "若条目 status=not_found，必须如实回答没有查询到对应订单或商品；若 status=failed/unavailable，只能说明暂时无法核实，不得编造缺失信息。\n"
                    + business_context.strip()
                ),
            }
        )
    messages.extend(history)
    return messages


def normalize_intent_result(data: Any, *, user_text: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}

    raw_intent = str(data.get("intent_code") or "unknown").strip() or "unknown"
    intent_code = raw_intent if raw_intent in KNOWN_INTENTS else "custom"
    confidence = _float_between(data.get("confidence"), 0, 1)
    resolution_status = str(data.get("resolution_status") or "").strip()
    if resolution_status not in VALID_RESOLUTION_STATUS:
        resolution_status = "need_human" if confidence < LOW_CONFIDENCE_THRESHOLD else "need_more_info"

    reply = clean_reply_text(str(data.get("reply") or "").strip())
    if not reply:
        reply = "亲，这个问题我先帮您记录下来，会尽快安排客服继续处理，请您稍等一下。"
        resolution_status = "need_human"

    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    actions = normalize_actions(data.get("actions"))
    default_action = INTENT_DEFAULT_ACTION.get(intent_code)
    if resolution_status == "need_action" and default_action and not any(item["type"] == default_action for item in actions):
        actions.append({"type": default_action, "payload": {"reason": "intent_default_action"}})
    needs_human = (
        confidence < LOW_CONFIDENCE_THRESHOLD
        or resolution_status == "need_human"
        # 只根据顾客原话判断是否点名人工，绝不能检查 LLM 自己生成的
        # reply 文本——客服回复里出现"人工客服"字样是正常话术，否则会
        # 大量误触发转人工。
        or _looks_like_human_request(user_text)
    )
    if needs_human and not any(item["type"] == "transfer_to_human" for item in actions):
        actions.append(
            {
                "type": "transfer_to_human",
                "payload": {
                    "reason": "low_confidence_or_need_human",
                    "confidence": confidence,
                    "resolution_status": resolution_status,
                },
            }
        )

    return {
        "reply": reply,
        "intent_code": intent_code,
        "raw_intent_code": raw_intent,
        "confidence": confidence,
        "resolution_status": resolution_status,
        "slots": slots,
        "actions": actions,
        "raw": data,
        "error": data.get("error"),
    }


def normalize_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            action_type = item
            payload = {}
        elif isinstance(item, dict):
            action_type = str(item.get("type") or item.get("action_type") or "").strip()
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        else:
            continue
        if action_type not in KNOWN_ACTIONS:
            action_type = "transfer_to_human"
            payload = {"reason": "unsupported_action", **payload}
        actions.append({"type": action_type, "payload": payload})
    return actions


def clean_reply_text(text: str) -> str:
    text = (text or "").strip()
    prefixes = (
        "文本消息",
        "图片消息",
        "商品卡片",
        "链接消息",
        "系统消息",
        "未知类型消息",
    )
    pattern = r"^\s*(?:\[(?:" + "|".join(re.escape(item) for item in prefixes) + r")\]\s*)+"
    previous = None
    while text and text != previous:
        previous = text
        text = re.sub(pattern, "", text).strip()
    return text


def _format_notes(notes: list[str] | None) -> str:
    if not notes:
        return ""
    lines: list[str] = []
    for index, note in enumerate(notes, start=1):
        content = str(note).strip()
        if not content:
            continue
        lines.append(f"{index}. {content}")
    return "\n".join(lines)


def _format_knowledge(hits: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, hit in enumerate(hits[:5], start=1):
        title = hit.get("knowledge_base_name") or hit.get("filename") or f"片段{index}"
        source = hit.get("source_label") or hit.get("filename") or ""
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{index}] {title} {source}\n{content[:1200]}")
    return "\n\n".join(lines)


def _float_between(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(low, min(high, number))


def _looks_like_human_request(user_text: Any) -> bool:
    text = str(user_text or "")
    return any(keyword in text for keyword in ("人工", "真人", "投诉", "举报", "平台介入", "别机器人"))
