import json
import re
from urllib.parse import urljoin

import requests

from openkefu.platforms.pdd.config import (
    BASE_URL,
    DEFAULT_USER_AGENT,
    HOME_URL,
    MMS_APP_JS_URL,
    MMS_COOKIE_NAME,
    MMS_LEON_SIDEBAR_TYPE,
    WS_USER_AGENT,
)


APP_JS_URL = MMS_APP_JS_URL
APP_JS_USER_AGENT = WS_USER_AGENT
DEFAULT_COOKIE_NAME = MMS_COOKIE_NAME
DEFAULT_LEON_SIDEBAR_TYPE = MMS_LEON_SIDEBAR_TYPE
COOKIE_NAME_RE = re.compile(r"""["'](mms_[a-f0-9]{8})["']""", re.IGNORECASE)
LEON_TYPE_RE = re.compile(
    r"""["']/merchant-web-service/leon["']\s*,\s*\{\s*type\s*:\s*["']([0-9a-f]{25,})["']""",
    re.IGNORECASE,
)
WHITELIST_RE = re.compile(r"^(!)?(get|post)\(([\w/?\-=]+)\)$")


class MmsB84d1838Builder:
    def __init__(self, session, cookies=None, anti_content=None, timeout=15):
        self.session = session
        self.cookies = dict(cookies or {})
        self.anti_content = anti_content
        self.timeout = timeout
        self.cookie_name = DEFAULT_COOKIE_NAME
        self.leon_sidebar_type = DEFAULT_LEON_SIDEBAR_TYPE
        self.app_js_etag = ""

    def build_cookie_value(self):
        self._load_app_js_config()
        sidebar = self._load_sidebar()
        groups = self._sidebar_groups(sidebar)
        visible_ids = []
        seen = set()

        for item in self._iter_whitelist_items(groups):
            item_id = item.get("id")
            if item_id in seen:
                continue
            if self._is_whitelist_visible(item):
                visible_ids.append(str(item_id))
                seen.add(item_id)

        return ",".join(visible_ids)

    def _load_sidebar(self):
        try:
            return self._get("/janus/api/pageResources/sidebar")
        except requests.RequestException:
            leon = self._post("/merchant-web-service/leon", {"type": self.leon_sidebar_type})
            value = leon.get("value") if isinstance(leon, dict) else None
            if isinstance(value, str) and value:
                return json.loads(value)
            return leon

    def _load_app_js_config(self):
        app_js = self._get_app_js()
        self.cookie_name = self._parse_cookie_name(app_js)
        self.leon_sidebar_type = self._parse_leon_sidebar_type(app_js)

    def _get_app_js(self):
        response = self.session.get(
            APP_JS_URL,
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "referer": f"{BASE_URL}/",
                "sec-ch-ua": "\"Microsoft Edge\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": "\"Windows\"",
                "user-agent": APP_JS_USER_AGENT,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        self.app_js_etag = getattr(response, "headers", {}).get("etag", "")
        return response.text

    @staticmethod
    def _parse_cookie_name(app_js):
        match = COOKIE_NAME_RE.search(app_js)
        return match.group(1) if match else DEFAULT_COOKIE_NAME

    @staticmethod
    def _parse_leon_sidebar_type(app_js):
        sidebar_pos = app_js.find("/janus/api/pageResources/sidebar")
        search_text = app_js[sidebar_pos: sidebar_pos + 30000] if sidebar_pos >= 0 else app_js
        match = LEON_TYPE_RE.search(search_text)
        return match.group(1) if match else DEFAULT_LEON_SIDEBAR_TYPE

    @staticmethod
    def _sidebar_groups(sidebar):
        if not isinstance(sidebar, dict):
            return []
        if isinstance(sidebar.get("sidebarUserViewList"), list):
            return sidebar["sidebarUserViewList"]
        if isinstance(sidebar.get("groups"), list):
            return sidebar["groups"]
        return []

    @staticmethod
    def _iter_whitelist_items(groups):
        for group in groups or []:
            for item in group.get("children") or []:
                if item.get("whitelist"):
                    yield item

    def _is_whitelist_visible(self, item):
        try:
            return self._eval_whitelist(item["whitelist"])
        except (requests.RequestException, ValueError, TypeError):
            return bool(item.get("defaultVisibility"))

    def _eval_whitelist(self, whitelist):
        match = WHITELIST_RE.match(whitelist or "")
        if not match:
            raise ValueError("whitelist parse error")

        reverse, method, uri = match.groups()
        result = self._get(uri) if method == "get" else self._post(uri, {})
        visible = self._to_int(result) == 1
        return not visible if reverse else visible

    def _get(self, uri):
        response = self.session.get(
            self._url(uri),
            cookies=self.cookies,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._unwrap_response(response.json())

    def _post(self, uri, body):
        response = self.session.post(
            self._url(uri),
            cookies=self.cookies,
            headers=self._headers(content_type="application/json"),
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._unwrap_response(response.json())

    @staticmethod
    def _url(uri):
        return uri if uri.startswith("http") else urljoin(BASE_URL, uri)

    def _headers(self, content_type=None):
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": HOME_URL,
            "sec-ch-ua": "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": DEFAULT_USER_AGENT,
        }
        if content_type:
            headers["content-type"] = content_type
            headers["origin"] = BASE_URL
        if self.anti_content:
            headers["anti-content"] = self.anti_content
        risk_control_fp = self.cookies.get("rckk") or self.cookies.get("_bee")
        if risk_control_fp:
            headers["etag"] = risk_control_fp
        return headers

    @staticmethod
    def _unwrap_response(data):
        if isinstance(data, dict) and "success" in data:
            if data.get("success"):
                return data.get("result")
            message = data.get("errorMsg") or data.get("error_msg") or "api error"
            raise requests.HTTPError(message)
        if isinstance(data, dict) and "result" in data and "data" in data:
            return data.get("data")
        return data

    @staticmethod
    def _to_int(value):
        if isinstance(value, dict):
            for key in ("result", "data", "value"):
                if key in value:
                    return MmsB84d1838Builder._to_int(value[key])
        return int(value)


def build_mms_b84d1838_context(session, cookies=None, anti_content=None, timeout=15):
    builder = MmsB84d1838Builder(
        session=session,
        cookies=cookies,
        anti_content=anti_content,
        timeout=timeout,
    )
    value = builder.build_cookie_value()
    return {
        "cookies": {builder.cookie_name: value} if value else {},
        "cookie_name": builder.cookie_name,
        "app_js_etag": builder.app_js_etag,
        "user_agent": APP_JS_USER_AGENT,
    }


def build_mms_b84d1838_cookie(session, cookies=None, anti_content=None, timeout=15):
    return build_mms_b84d1838_context(
        session=session,
        cookies=cookies,
        anti_content=anti_content,
        timeout=timeout,
    )["cookies"]
