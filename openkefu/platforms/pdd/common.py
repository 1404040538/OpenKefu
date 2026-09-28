import json

import requests

from openkefu.platforms.pdd.config import BASE_URL, DEFAULT_USER_AGENT, HOME_URL, LOGIN_URL


# 前端会让同一含义的新旧 cookie 名保持相同的值。
COOKIE_ALIAS_GROUPS = [
    ("rckk", "_bee"),
    ("ru1k", "_f77"),
    ("ru2k", "_a42"),
]

KNOWN_COOKIE_ORDER = [
    "api_uid",
    "_nano_fp",
    "rckk",
    "_bee",
    "ru1k",
    "_f77",
    "ru2k",
    "_a42",
    "mms_b84d1838",
    "x-visit-time",
    "PASS_ID",
    "JSESSIONID",
    "windows_app_shop_token_23",
]


def normalize_cookie_aliases(cookies):
    normalized = dict(cookies or {})
    for names in COOKIE_ALIAS_GROUPS:
        value = next((normalized[name] for name in names if normalized.get(name)), "")
        if not value:
            continue
        for name in names:
            normalized[name] = value
    return normalized


def merge_session_cookies(local_cookies, session):
    cookies = dict(local_cookies or {})
    cookies.update(requests.utils.dict_from_cookiejar(session.cookies))
    return normalize_cookie_aliases(cookies)


def known_cookies(cookies):
    ordered = {}
    for name in KNOWN_COOKIE_ORDER:
        if cookies.get(name):
            ordered[name] = cookies[name]
    for name, value in cookies.items():
        if value and name not in ordered:
            ordered[name] = value
    return ordered


def cookie_string(cookies):
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def risk_control_fp(cookies):
    return (cookies or {}).get("_bee") or (cookies or {}).get("rckk") or ""


def json_request_data(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}
