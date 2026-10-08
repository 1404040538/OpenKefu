from __future__ import annotations

# 两个运行时（pdd/qianniu）的 _llm_history 各自最多取 10~11 条，
# 这里做统一的防御性截断，超出部分直接丢弃（不调用 LLM 做摘要）。
MAX_HISTORY_MESSAGES = 12


def build_conversation_messages(
    history: list[dict[str, str]],
    max_messages: int = MAX_HISTORY_MESSAGES,
) -> list[dict[str, str]]:
    if len(history) <= max_messages:
        return history
    return history[-max_messages:]
