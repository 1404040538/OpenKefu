# -*- coding: utf-8 -*-
"""千牛（淘宝商家）平台公共工具。"""

import json


def cookie_string(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return {"_raw": response.text}


def parse_jsonp(text: str) -> dict:
    """解析 `(function(){jsonpN({...});})();` 形式的 JSONP 响应。"""
    import json
    import re

    match = re.search(r"jsonp\d+\(", text)
    if not match:
        raise ValueError(f"non-jsonp response: {text[:200]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text, match.end())
    return obj


def session_cookie_dict(session) -> dict:
    return {c.name: c.value for c in session.cookies}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
