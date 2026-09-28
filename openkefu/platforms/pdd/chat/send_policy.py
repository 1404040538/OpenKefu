"""Pure response policy shared by manual and automatic message delivery."""
from __future__ import annotations

import re
from typing import Any


def _response_data(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def send_message_response_ok(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("success") is False:
        return False
    data = _response_data(result)
    return (
        data.get("success") is not False
        and data.get("response") == "send_message"
        and data.get("result") == "ok"
        and data.get("msg_id") not in (None, "")
    )


def send_message_failure_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    data = _response_data(result)
    code = next(
        (item[key] for item in (data, result) for key in ("error_code", "errorCode", "code")
         if item.get(key) is not None),
        None,
    )
    message = next(
        (item[key] for item in (data, result)
         for key in ("error_msg", "errorMsg", "message", "msg") if item.get(key)),
        data.get("result") or "invalid send_message response",
    )
    return f"code={code}, message={message}" if code is not None else str(message)


def is_session_expired(error: Any) -> bool:
    """Recognize the platform's rejection; network and local DB errors are not expiry."""
    if str(getattr(error, "error_code", "")) == "43001":
        return True
    response = error if isinstance(error, dict) else getattr(error, "response", None)
    text = send_message_failure_text(response) if isinstance(response, dict) else str(error)
    return bool(re.search(r"(?<!\d)43001(?!\d)", text)) or any(
        marker in text.lower() for marker in ("会话已过期", "session expired")
    )
