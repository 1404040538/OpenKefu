from __future__ import annotations

from openkefu.platforms.pdd.chat.llm import LLMClient

SUMMARIZE_PROMPT = (
    "你是一个对话摘要助手。请用一句话（不超过100字）概括以下客服对话的要点，"
    "包括顾客的主要诉求、订单号（如有）、以及已达成的结论。直接输出摘要文本，不要加任何前缀。"
)

MAX_HISTORY_MESSAGES = 10
SUMMARIZE_THRESHOLD = 16


def build_conversation_messages(
    history: list[dict[str, str]],
    llm_client: LLMClient | None = None,
    *,
    max_messages: int = MAX_HISTORY_MESSAGES,
    threshold: int = SUMMARIZE_THRESHOLD,
) -> list[dict[str, str]]:
    if len(history) <= max_messages:
        return history

    recent = history[-max_messages:]
    early = history[:-max_messages]

    if len(early) < threshold - max_messages and len(history) <= threshold:
        return history

    summary = _generate_summary(early, llm_client) if llm_client else _extractive_summary(early)
    return [{"role": "system", "content": f"[对话历史摘要] {summary}"}] + recent


def _generate_summary(early: list[dict[str, str]], llm_client: LLMClient) -> str:
    dialogue = "\n".join(
        f"{'客服' if item.get('role') == 'assistant' else '顾客'}: {item.get('content', '')}"
        for item in early
    )
    try:
        messages = [
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {"role": "user", "content": dialogue},
        ]
        result = llm_client.chat(messages, max_tokens=200, temperature=0.0, max_retries=0)
        return result.strip()
    except Exception:
        return _extractive_summary(early)


def _extractive_summary(early: list[dict[str, str]]) -> str:
    user_msgs = [item["content"] for item in early if item.get("role") == "user"]
    preview = "；".join(user_msgs[-3:])
    return preview[:200] if preview else "（历史对话）"
