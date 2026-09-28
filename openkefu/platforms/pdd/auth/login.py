import json
import logging
import time
import re
import random
from http.cookies import SimpleCookie

import requests
import qrcode as qrcode_lib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import base64

from openkefu.platforms.pdd.auth.mms_b84d1838 import build_mms_b84d1838_context
from openkefu.platforms.pdd.auth.password_js_bridge import PasswordJsBridge
from openkefu.platforms.pdd.auth.pfb_codec import build_pfb_body_from_template, load_template
from openkefu.platforms.pdd.auth.qrcode_js_bridge import QrcodeJsBridge
from openkefu.platforms.pdd.common import (
    DEFAULT_USER_AGENT,
    HOME_URL,
    LOGIN_URL,
    cookie_string,
    json_or_text,
    json_request_data,
    known_cookies,
    merge_session_cookies,
    normalize_cookie_aliases,
    risk_control_fp,
)
from openkefu.platforms.pdd.config import PASSWORD_LOGIN_JS, PFB_TEMPLATE_FILE, QRCODE_API_JS, QRCODE_DIR


logger = logging.getLogger(__name__)


class CaptchaRequiredError(RuntimeError):
    """登录需要图形验证码（PDD 54001），无自动通过通道，需人工介入或稍后重试。"""

    def __init__(self, *, code_status=None, error_msg="", verify_auth_token=None):
        self.code_status = code_status
        self.error_msg = error_msg
        self.verify_auth_token = verify_auth_token
        super().__init__(f"login requires captcha (codeStatus={code_status}): {error_msg}")


def _login_response_summary(result):
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    keys = (
        "status",
        "codeStatus",
        "errorCode",
        "errorMsg",
        "isSuccess",
        "isBusy",
        "verifyAuthToken",
    )
    summary = {key: result.get(key) for key in keys if key in result and key != "verifyAuthToken"}
    summary["hasVerifyAuthToken"] = bool(result.get("verifyAuthToken"))
    return summary

PASSWORD_LOGIN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
]


class Login:
    def __init__(self):
        self.session = requests.Session()
        self.js = QrcodeJsBridge(QRCODE_API_JS)
        self.pwd_js = PasswordJsBridge(PASSWORD_LOGIN_JS)
        self.pfb_template_file = PFB_TEMPLATE_FILE
        self.login_url = LOGIN_URL
        self.home_url = HOME_URL
        self.qrcode_dir = QRCODE_DIR
        self.cookies = {}
        self.anti_content = None
        self.base_headers = {}
        self._pfb_template = None
        self.fingerprint_env = self.js.randomize_fingerprint()
        self._rotate_fingerprint_user_agent()

    # ---------- 共享状态辅助方法 ----------

    def _rotate_fingerprint_user_agent(self) -> str:
        current = self._current_user_agent()
        candidates = [ua for ua in PASSWORD_LOGIN_USER_AGENTS if ua != current] or PASSWORD_LOGIN_USER_AGENTS
        ua = random.choice(candidates)
        env = dict(self.fingerprint_env or {})
        navigator = dict(env.get("navigator") or {})
        navigator["ua"] = ua
        env["navigator"] = navigator
        self.fingerprint_env = env
        return ua

    def _current_user_agent(self) -> str:
        navigator = self.fingerprint_env.get("navigator") if isinstance(self.fingerprint_env, dict) else {}
        return str((navigator or {}).get("ua") or DEFAULT_USER_AGENT)

    def _current_chrome_major(self) -> str:
        match = re.search(r"Chrome/(\d+)", self._current_user_agent())
        return match.group(1) if match else "147"

    def _current_sec_ch_ua(self) -> str:
        major = self._current_chrome_major()
        ua = self._current_user_agent()
        if "Edg/" in ua:
            return f'"Chromium";v="{major}", "Microsoft Edge";v="{major}", "Not/A)Brand";v="99"'
        return f'"Google Chrome";v="{major}", "Not.A/Brand";v="8", "Chromium";v="{major}"'

    def rotate_login_fingerprint(self) -> dict:
        self.session = requests.Session()
        self.cookies = {}
        self.anti_content = None
        self.base_headers = {}
        self.fingerprint_env = self.js.randomize_fingerprint()
        self._rotate_fingerprint_user_agent()
        logger.info("rotated login fingerprint ua=%s", self._current_user_agent())
        return self.fingerprint_env

    def _update_cookie_store(self, cookies=None, **extra):
        # Refreshes can arrive under either alias or in a JSON response body.
        # Normalize the incoming values first so an old alias cannot win.
        updates = dict(cookies or {})
        updates.update({key: value for key, value in extra.items() if value})
        updates = normalize_cookie_aliases(updates)
        merged = dict(self.cookies)
        merged.update(updates)
        self.cookies = normalize_cookie_aliases(merged)
        # Requests also sends its CookieJar, then _session_cookies reads it back.
        # Keep existing scoped cookies current without adding domainless duplicates
        # or dropping their domain/path/security attributes.
        for cookie in self.session.cookies:
            if cookie.name in updates:
                cookie.value = updates[cookie.name]
        return self.cookies

    def _session_cookies(self):
        self.cookies = merge_session_cookies(self.cookies, self.session)
        return self.cookies

    def _known_cookies(self):
        return known_cookies(self._session_cookies())

    def _save_request_context(self, req):
        self._update_cookie_store(req["cookies"])
        headers = req.get("headers") or {}
        self.anti_content = headers.get("Anti-Content") or headers.get("anti-content") or self.anti_content

    def _request_json(self, req, *, timeout=15):
        self._save_request_context(req)
        response = self.session.request(
            req["method"],
            req["url"],
            cookies=req["cookies"],
            headers=req["headers"],
            data=json_request_data(req["body"]) if req.get("body") is not None else None,
            timeout=timeout,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        return json_or_text(response)

    def _merge_response_cookies(self, response):
        updates = {}
        try:
            updates.update(requests.utils.dict_from_cookiejar(response.cookies))
        except Exception:
            pass

        set_cookie_values = []
        raw_headers = getattr(getattr(response, "raw", None), "headers", None)
        if raw_headers and hasattr(raw_headers, "get_all"):
            set_cookie_values.extend(raw_headers.get_all("Set-Cookie") or [])
        header_value = response.headers.get("Set-Cookie")
        if header_value:
            set_cookie_values.append(header_value)

        for value in set_cookie_values:
            cookie = SimpleCookie()
            try:
                cookie.load(value)
            except Exception:
                continue
            for name, morsel in cookie.items():
                if morsel.value:
                    updates[name] = morsel.value

        self._update_cookie_store(self._session_cookies())
        if updates:
            self._update_cookie_store(updates)
        return self.cookies

    # ---------- Anti-Content 辅助方法 ----------

    def refresh_anti_content(self, fingerprint_env=None):
        """根据当前 cookies 生成一份新的 Anti-Content。"""
        cookies = self._session_cookies() or self.init_login_cookies()
        self.anti_content = self.js.anti_content(cookies, fingerprint_env=fingerprint_env)
        return self.anti_content

    def get_latest_anti_content(self, location_href=None, fingerprint_env=None):
        if fingerprint_env is None:
            fingerprint_env = self.fingerprint_env
        env = dict(fingerprint_env or {})
        if location_href:
            env["locationHref"] = location_href
        return self.refresh_anti_content(env or None)

    def build_anti_headers(self, location_href=None, *, fingerprint_env=None, include_cookie=False):
        """为任意 MMS 请求构造风控请求头。

        Anti-Content 与时间、cookie 和浏览器环境相关，因此每次调用都重新生成。
        """
        cookies = self._session_cookies() or self.init_login_cookies()
        anti_content = self.get_latest_anti_content(
            location_href or self.home_url,
            fingerprint_env=fingerprint_env,
        )
        headers = {
            "anti-content": anti_content,
            "etag": risk_control_fp(cookies),
            "user-agent": self._current_user_agent(),
        }
        if include_cookie:
            headers["Cookie"] = cookie_string(self._known_cookies())
        return {key: value for key, value in headers.items() if value}

    def request_with_latest_anti(
        self,
        method,
        url,
        *,
        headers=None,
        location_href=None,
        fingerprint_env=None,
        timeout=15,
        **kwargs,
    ):
        """发送请求，并强制携带最新生成的 Anti-Content。"""
        cookies = kwargs.pop("cookies", None) or self._session_cookies() or self.init_login_cookies()
        anti_headers = self.build_anti_headers(location_href or self.home_url, fingerprint_env=fingerprint_env)
        request_headers = dict(anti_headers)
        request_headers.update(headers or {})
        request_headers["anti-content"] = anti_headers["anti-content"]
        response = self.session.request(
            method,
            url,
            cookies=cookies,
            headers=request_headers,
            timeout=timeout,
            **kwargs,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        return response

    # ---------- 登录 cookie 初始化 ----------

    def init_login_cookies(self):
        """打开登录页，补齐浏览器侧需要的初始 cookies。"""
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": "https://mms.pinduoduo.com/",
            "user-agent": self._current_user_agent(),
        }
        response = self.session.get(self.login_url, headers=headers, timeout=15)
        response.raise_for_status()
        self._update_cookie_store(self._session_cookies())
        self._update_cookie_store(self.js.build_cookies(self.cookies))
        return self.cookies

    # ---------- pfb/a2 上报 ----------

    def _load_pfb_template(self):
        if self._pfb_template is None:
            self._pfb_template = load_template(self.pfb_template_file)
        return self._pfb_template

    def _build_pfb_a2_request(self, cookies):
        context = self.js.run("build-pfb-context", {
            "cookies": cookies,
            "fingerprintEnv": self.fingerprint_env,
        })
        template = self._load_pfb_template()
        body = build_pfb_body_from_template(
            template["encrypted_data"],
            updates=context.get("updates"),
            raw_data_updates=context.get("rawDataUpdates"),
            timestamp=context["timestamp"],
        )
        return {
            "method": context["method"],
            "url": context["url"],
            "headers": context["headers"],
            "cookies": context["cookies"],
            "body": {
                "data": body["data"],
                "timestamp": body["timestamp"],
                "appKey": body["appKey"],
                "sign": body["sign"],
            },
        }

    def _apply_pfb_result(self, result):
        mapping = {
            "a": ("rckk", "_bee"),
            "b": ("ru1k", "_f77"),
            "c": ("ru2k", "_a42"),
        }
        updates = {}
        for key, names in mapping.items():
            value = result.get(key)
            if isinstance(value, list):
                value = value[0] if value else ""
            if not value:
                continue
            for name in names:
                updates[name] = value
        self._update_cookie_store(updates)
        return updates

    def report_pfb_a2(self):
        cookies = self._session_cookies() or self.init_login_cookies()
        data = self._request_json(self._build_pfb_a2_request(cookies))
        self._apply_pfb_result(data.get("result") or {})
        return data

    # ---------- 扫码登录流程 ----------

    def get_qrcode(self, *, retry_count=3):
        """获取登录二维码。

        遇到 PDD 54001（需要图形验证码）= 当前设备环境不被信任的信号：
        → 轮换全新设备环境（指纹 + UA + session + cookies 基线 + pfb 上报）后重试；
        → 全部环境均被要求验证码时才抛 CaptchaRequiredError（极小概率）。
        """
        last_exc = None
        for attempt in range(retry_count + 1):
            try:
                self._session_cookies() or self.init_login_cookies()
                self.report_pfb_a2()
                cookies = self._session_cookies()
                build_opts = {
                    "cookies": cookies,
                    "antiContent": self.get_latest_anti_content(self.login_url),
                    "fingerprintEnv": self.fingerprint_env,
                }
                req = self.js.run("build-qrcode", build_opts)
                result = self.js.run("parse-qrcode", self._request_json(req))
                if result and result.get("token"):
                    return result
                code_status = result.get("codeStatus") if isinstance(result, dict) else None
                error_msg = result.get("errorMsg") if isinstance(result, dict) else ""
                if code_status == 54001 or "图形验证码" in str(error_msg or ""):
                    if attempt < retry_count:
                        # 环境评分不足 → 刷新全新设备环境（指纹/UA/session/cookies）后重试
                        logger.warning(
                            "get_qrcode attempt %d hit captcha challenge; rotating fresh device env and retrying",
                            attempt + 1,
                        )
                        time.sleep(3.0)
                        self.rotate_login_fingerprint()
                        continue
                    raise CaptchaRequiredError(
                        code_status=code_status,
                        error_msg=error_msg,
                        verify_auth_token=result.get("verifyAuthToken") if isinstance(result, dict) else None,
                    )
                logger.warning(
                    "get_qrcode attempt %d returned no token: codeStatus=%s errorCode=%s errorMsg=%s verifyAuthToken=%s",
                    attempt + 1,
                    result.get("codeStatus") if result else None,
                    result.get("errorCode") if result else None,
                    result.get("errorMsg") if result else None,
                    (result.get("verifyAuthToken") or "")[:20] + "..." if result and result.get("verifyAuthToken") else None,
                )
                if attempt < retry_count:
                    time.sleep(3.0)
                    self.rotate_login_fingerprint()
                    continue
            except CaptchaRequiredError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < retry_count:
                    logger.warning("get_qrcode attempt %d failed: %s, retrying...", attempt + 1, exc)
                    time.sleep(3.0)
                    self.rotate_login_fingerprint()
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("get_qrcode failed: no token returned after retries")

    def query_qrcode(self, token, *, retry_count=2, verify_auth_token=None):
        if not token:
            raise ValueError("token is required for query_qrcode")
        
        cookies = self._session_cookies() or self.init_login_cookies()
        
        for attempt in range(retry_count + 1):
            try:
                build_opts = {
                    "token": token,
                    "cookies": cookies,
                    "antiContent": self.get_latest_anti_content(self.login_url),
                    "fingerprintEnv": self.fingerprint_env,
                }
                if verify_auth_token:
                    build_opts["verifyAuthToken"] = verify_auth_token
                req = self.js.run("build-query", build_opts)
                result = self.js.run("parse-query", self._request_json(req))
                return result
            except Exception as exc:
                if attempt < retry_count:
                    logger.warning("query_qrcode attempt %d failed: %s, retrying...", attempt + 1, exc)
                    time.sleep(0.5)
                    cookies = self._session_cookies() or self.init_login_cookies()
                    continue
                logger.error("query_qrcode failed after %d attempts: %s", retry_count + 1, exc)
                raise

    def wait_for_login_and_get_target_cookies(self, token, *, poll_interval=2, timeout=180, on_status=None):
        deadline = time.time() + timeout
        last_result = None
        verify_auth_token = None
        while time.time() < deadline:
            last_result = self.query_qrcode(token, verify_auth_token=verify_auth_token)
            if on_status:
                on_status(last_result)
            if last_result.get("isSuccess") or last_result.get("status") == 3:
                return {
                    "query": last_result,
                    **self.get_target_cookies(),
                }
            if last_result.get("isBusy") or last_result.get("status") == 5:
                raise RuntimeError(f"qrcode login busy: {json.dumps(_login_response_summary(last_result), ensure_ascii=False)}")
            if last_result.get("verifyAuthToken"):
                verify_auth_token = last_result["verifyAuthToken"]
            time.sleep(poll_interval)

        raise TimeoutError(f"wait for qrcode login timeout: {json.dumps(_login_response_summary(last_result), ensure_ascii=False)}")

    def get_qrcode_and_wait_for_login(self, *, poll_interval=2, timeout=180):
        qrcode_info = self.get_qrcode()
        qrcode_path = self.qrcode_dir / f"login_qrcode_{int(time.time())}.png"
        self.qrcode_dir.mkdir(parents=True, exist_ok=True)
        qrcode_lib.make(qrcode_info["uri"]).save(qrcode_path)
        logger.info("login qrcode generated path=%s", qrcode_path)

        result = self.wait_for_login_and_get_target_cookies(
            qrcode_info["token"],
            poll_interval=poll_interval,
            timeout=timeout,
        )
        return {
            "qrcode": qrcode_info,
            "qrcode_path": str(qrcode_path),
            "query": result["query"],
            "cookies": result["cookies"],
            "cookie_string": result["cookie_string"],
            "requests_headers": result["requests_headers"],
            "base_headers": result.get("base_headers", {}),
        }

    # ---------- 登录后 cookie 补齐 ----------

    def ensure_windows_app_shop_token_23(self, sub_system_id=23):
        cookies = self._session_cookies() or self.init_login_cookies()
        req = self.js.run("build-subsystem-auth-token", {
            "cookies": cookies,
            "antiContent": self.get_latest_anti_content(self.home_url),
            "subSystemId": sub_system_id,
            "fingerprintEnv": self.fingerprint_env,
        })
        data = self._request_json(req)
        parsed = self.js.run("parse-subsystem-auth-token", {
            "cookies": self._session_cookies(),
            "response": data,
        })
        parsed_cookies = dict(parsed["cookies"])
        if int(sub_system_id) != 23:
            # The JS parser uses the shop-token name for every subsystem.
            # A Titan token must never replace the HTTP shop authentication cookie.
            parsed_cookies.pop("windows_app_shop_token_23", None)
        self._update_cookie_store(parsed_cookies)
        return parsed.get("authToken") or ""

    def user_info(self):
        cookies = self._session_cookies()
        headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "anti-content": self.get_latest_anti_content(self.login_url),
            "cache-control": "no-cache",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://mms.pinduoduo.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": self.login_url,
            "sec-ch-ua": self._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._current_user_agent(),
        }
        response = self.session.post(
            "https://mms.pinduoduo.com/janus/api/new/userinfo",
            cookies=cookies,
            headers=headers,
            json={},
            timeout=15,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        return response.json()

    def _post_login_warmup_request(self, method: str, url: str, *, body=None, referer=None):
        cookies = self._session_cookies() or self.init_login_cookies()
        location_href = referer or self.home_url
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "anti-content": self.get_latest_anti_content(location_href),
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": location_href,
            "sec-ch-ua": self._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._current_user_agent(),
        }
        if risk_control_fp(cookies):
            headers["etag"] = risk_control_fp(cookies)
        if method.upper() == "POST":
            headers["content-type"] = "application/json;charset=UTF-8"
            headers["origin"] = "https://mms.pinduoduo.com"

        response = self.session.request(
            method,
            url,
            cookies=cookies,
            headers=headers,
            json=body if body is not None else None,
            timeout=15,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        return json_or_text(response)

    def import_new_status(self):
        return self._post_login_warmup_request(
            "GET",
            "https://mms.pinduoduo.com/earth/api/merchant/importNewStatus",
            referer=self.login_url,
        )

    def user_server_connect(self):
        return self._post_login_warmup_request(
            "POST",
            "https://mms.pinduoduo.com/janus/api/user/server/connect",
            body={},
            referer=self.login_url,
        )

    def legacy_userinfo(self):
        return self._post_login_warmup_request(
            "POST",
            "https://mms.pinduoduo.com/janus/api/userinfo",
            body={},
            referer=self.login_url,
        )

    def common_mall_info(self):
        cookies = self._session_cookies() or self.init_login_cookies()
        req = self.js.run("build-common-mall-info", {
            "cookies": cookies,
            "antiContent": self.get_latest_anti_content(self.home_url),
            "fingerprintEnv": self.fingerprint_env,
        })
        return self._request_json(req)

    def get_default_refund_address(self):
        cookies = self._session_cookies() or self.init_login_cookies()
        if not cookies.get("x-visit-time"):
            cookies = self._update_cookie_store(**{"x-visit-time": str(int(time.time() * 1000))})

        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "anti-content": self.get_latest_anti_content("https://mms.pinduoduo.com/login/"),
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://mms.pinduoduo.com/login/",
            "sec-ch-ua": self._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._current_user_agent(),
        }
        if risk_control_fp(cookies):
            headers["etag"] = risk_control_fp(cookies)

        response = self.session.get(
            "https://mms.pinduoduo.com/antis/api/refundAddress/getDefaultRefundAddress",
            cookies=cookies,
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        return json_or_text(response)

    def ensure_mms_b84d1838(self):
        cookies = self._session_cookies() or self.init_login_cookies()
        context = build_mms_b84d1838_context(
            self.session,
            cookies=cookies,
            anti_content=self.get_latest_anti_content(self.home_url),
        )
        updates = context["cookies"]
        self._update_cookie_store(self._session_cookies())
        self._update_cookie_store(updates)
        self.base_headers = {
            **self.build_anti_headers(self.home_url),
            "user-agent": context["user_agent"],
        }
        return next(iter(updates.values()), "")

    def get_target_cookies(self):
        self._session_cookies() or self.init_login_cookies()
        try:
            self.import_new_status()
        except requests.RequestException:
            pass
        self.user_info()
        self.get_default_refund_address()
        for warmup in (self.user_server_connect, self.legacy_userinfo):
            try:
                warmup()
            except requests.RequestException:
                pass
        self.ensure_windows_app_shop_token_23()
        try:
            self.common_mall_info()
        except requests.RequestException:
            # 部分账号无法读取 commonMallInfo；前置接口返回的 cookies 仍然有效，
            # 因此这个补充步骤失败时不阻断登录流程。
            pass
        self.ensure_mms_b84d1838()
        return self._format_cookie_bundle(self._known_cookies())

    # ---------- 账密登录流程 ----------

    def _rsa_encrypt_password(self, password: str, public_key_pem: str) -> str:
        """使用 RSA 公钥加密密码，返回 Base64 字符串。"""
        try:
            public_key = serialization.load_pem_public_key(
                public_key_pem.encode("utf-8"), backend=default_backend()
            )
        except Exception:
            # PDD 的公钥可能没有标准的 PEM 头尾，尝试补全
            pem = (
                "-----BEGIN PUBLIC KEY-----\n"
                + public_key_pem
                + "\n-----END PUBLIC KEY-----"
            )
            public_key = serialization.load_pem_public_key(
                pem.encode("utf-8"), backend=default_backend()
            )

        encrypted = public_key.encrypt(
            password.encode("utf-8"),
            padding.PKCS1v15(),
        )
        return base64.b64encode(encrypted).decode("utf-8")

    def _query_password_encrypt_config(self) -> dict:
        """查询账密登录配置，获取 publicKey 和 passwordEncrypt 标志。"""
        cookies = self._session_cookies() or self.init_login_cookies()
        req = self.pwd_js.query_password_encrypt_request(
            cookies=cookies,
            antiContent=self.get_latest_anti_content(self.login_url),
            fingerprintEnv=self.fingerprint_env,
        )
        data = self._request_json(req)
        # 接口返回格式：{success, errorCode, errorMsg, result: {passwordEncrypt, publicKey}}
        return (data.get("result") or {}) if isinstance(data, dict) else {}

    def do_password_login(
        self, username: str, password: str, verify_code: str = "", *, captcha_retry_count: int = 2
    ) -> dict:
        """通过账密方式登录 PDD。

        返回 dict，可能包含以下字段：
        - 登录成功：返回完整的 cookies 信息
        - 需要验证：{"need_verify": True, "verify_type": "mobile"/"captcha", "mask_mobile": "...", "error_code": ...}

        verify_code: 短信/图形验证码（第二次调用时传入，同时设置 verificationCode 和 mobileVerifyCode）
        """
        cookies = self._session_cookies() or self.init_login_cookies()
        self.report_pfb_a2()

        # 查询加密配置
        config = self._query_password_encrypt_config()
        password_encrypt = config.get("passwordEncrypt", False)
        public_key = config.get("publicKey", "")

        # 加密密码
        encrypted_password = password
        if password_encrypt and public_key:
            encrypted_password = self._rsa_encrypt_password(password, public_key)
            logger.info("password encrypted (RSA)")

        timestamp = int(time.time() * 1000)

        # 浏览器实际提交的 riskSign 是把登录参数串再做一次 RSA 加密。
        sign_timestamp = timestamp
        risk_plain = f"username={username}&password={password}&ts={sign_timestamp}"
        if public_key:
            risk_sign = self._rsa_encrypt_password(risk_plain, public_key)
        else:
            risk_sign_result = self.pwd_js.build_risk_sign(username, password, sign_timestamp)
            risk_sign = risk_sign_result.get("riskSign", "")
            sign_timestamp = risk_sign_result.get("timestamp", sign_timestamp)

        # 获取 anti-content
        cookies = self._session_cookies()
        anti_content = self.get_latest_anti_content(self.login_url)

        touchevent = {
            "mobileInputEditStartTime": timestamp,
            "mobileInputEditFinishTime": timestamp,
            "mobileInputKeyboardEvent": "0|0|0|",
            "passwordInputEditStartTime": timestamp,
            "passwordInputEditFinishTime": timestamp,
            "passwordInputKeyboardEvent": "0|0|0|",
            "captureInputEditStartTime": "",
            "captureInputEditFinishTime": "",
            "captureInputKeyboardEvent": "",
            "loginButtonTouchPoint": "1056,650",
            "loginButtonClickTime": timestamp,
        }

        auth_body = {
            "username": username,
            "password": encrypted_password,
            "passwordEncrypt": bool(password_encrypt and public_key),
            "verificationCode": verify_code,
            "mobileVerifyCode": verify_code,
            "sign": "",
            "touchevent": touchevent,
            "fingerprint": {
                "innerHeight": self.fingerprint_env.get("innerHeight", 945),
                "innerWidth": self.fingerprint_env.get("innerWidth", 1430),
                "devicePixelRatio": self.fingerprint_env.get("devicePixelRatio", 1),
                "availHeight": self.fingerprint_env.get("availHeight", 1032),
                "availWidth": self.fingerprint_env.get("availWidth", 1920),
                "height": self.fingerprint_env.get("height", 1080),
                "width": self.fingerprint_env.get("width", 1920),
                "colorDepth": self.fingerprint_env.get("colorDepth", 32),
                "locationHref": self.login_url,
                "clientWidth": self.fingerprint_env.get("innerWidth", 1430),
                "clientHeight": self.fingerprint_env.get("innerHeight", 945),
                "offsetWidth": self.fingerprint_env.get("innerWidth", 1430),
                "offsetHeight": self.fingerprint_env.get("innerHeight", 945),
                "scrollWidth": self.fingerprint_env.get("scrollWidth", 2743),
                "scrollHeight": self.fingerprint_env.get("innerHeight", 945),
                "navigator": self.fingerprint_env.get("navigator", {}),
                "referer": "https://mms.pinduoduo.com/home",
                "timezoneOffset": self.fingerprint_env.get("timezoneOffset", -480),
            },
            "riskSign": risk_sign,
            "timestamp": sign_timestamp,
            "crawlerInfo": anti_content,
        }

        url = "https://mms.pinduoduo.com/janus/api/auth"
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "anti-content": anti_content,
            "cache-control": "no-cache",
            "content-type": "application/json",
            "etag": risk_control_fp(cookies),
            "origin": "https://mms.pinduoduo.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": self.login_url,
            "sec-ch-ua": self._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._current_user_agent(),
        }

        response = self.session.post(
            url,
            cookies=cookies,
            headers=headers,
            json=auth_body,
            timeout=15,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)

        result = response.json()
        auth_result = result.get("result") or {}
        logger.info(
            "password auth response: user=%s success=%s errorCode=%s authResult=%s "
            "mobileVerification=%s forceMobileVerify=%s hasPassIdBody=%s hasPassIdCookie=%s "
            "identityVerifyURL=%s loginLimitStatus=%s",
            username,
            result.get("success"),
            result.get("errorCode"),
            auth_result.get("authResult") if isinstance(auth_result, dict) else None,
            auth_result.get("mobileVerification") if isinstance(auth_result, dict) else None,
            auth_result.get("forceMobileVerify") if isinstance(auth_result, dict) else None,
            bool(auth_result.get("passId")) if isinstance(auth_result, dict) else False,
            bool(self._session_cookies().get("PASS_ID")),
            bool(auth_result.get("identityVerifyURL")) if isinstance(auth_result, dict) else False,
            auth_result.get("loginLimitStatus") if isinstance(auth_result, dict) else None,
        )

        if result.get("success"):
            pass_id = ""
            if isinstance(auth_result, dict):
                pass_id = auth_result.get("passId") or ""
            pass_id = pass_id or self._session_cookies().get("PASS_ID", "")
            force_mobile_verify = isinstance(auth_result, dict) and (
                auth_result.get("forceMobileVerify")
                or auth_result.get("identityVerifyURL")
                or auth_result.get("loginLimitStatus")
            )
            if force_mobile_verify and not pass_id:
                mask_mobile = (
                    auth_result.get("maskMobile")
                    or (auth_result.get("userInfoVO") or {}).get("mobile")
                    or ""
                )
                logger.info("password login requires mobile verification: user=%s", username)
                return {
                    "need_verify": True,
                    "verify_type": "mobile",
                    "mask_mobile": str(mask_mobile),
                    "error_code": result.get("errorCode", 1000000),
                }

            if pass_id:
                self._update_cookie_store(PASS_ID=pass_id)

            logger.info("password login success: user=%s", username)
            return self.get_target_cookies()

        error_code = result.get("errorCode", 0)

        # 需要手机验证
        if error_code == 2000020:
            mask_mobile = str(result.get("result") or "")
            return {
                "need_verify": True,
                "verify_type": "mobile",
                "mask_mobile": mask_mobile,
                "error_code": error_code,
            }

        # 需要图形验证码
        if error_code == 54001:
            if not verify_code and captcha_retry_count > 0:
                logger.warning(
                    "password login requires captcha; rotating UA and retrying user=%s retries_left=%s",
                    username,
                    captcha_retry_count,
                )
                self.rotate_login_fingerprint()
                return self.do_password_login(
                    username,
                    password,
                    verify_code,
                    captcha_retry_count=captcha_retry_count - 1,
                )
            return {
                "need_verify": True,
                "verify_type": "captcha",
                "message": "需要图形验证码，已尝试切换 UA 后仍未通过",
                "ua": self._current_user_agent(),
                "verify_auth_token": result.get("result", {}).get("verifyAuthToken", ""),
                "error_code": error_code,
            }

        error_msg = result.get("errorMsg") or "账密登录失败"
        raise RuntimeError(f"password login failed [{error_code}]: {error_msg}")

    def send_mobile_verify_code(self, username: str, mobile: str = "") -> bool:
        """发送登录手机验证码。

        需要在 do_password_login 返回 need_verify 后调用。
        使用 /janus/api/user/getLoginVerificationCode 而非 sendVerificationCode/noLogin，
        后者是修改绑定手机号的接口，收到的短信提示不同。
        """
        cookies = self._session_cookies() or self.init_login_cookies()

        body = {
            "username": username,
        }

        headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "anti-content": self.get_latest_anti_content(self.login_url),
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://mms.pinduoduo.com",
            "referer": self.login_url,
            "sec-ch-ua": self._current_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self._current_user_agent(),
        }

        response = self.session.post(
            "https://mms.pinduoduo.com/janus/api/user/getLoginVerificationCode",
            cookies=cookies,
            headers=headers,
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        self._merge_response_cookies(response)
        result = response.json()
        return result.get("success", False)

    # ---------- 结果格式化 ----------

    def _format_cookie_bundle(self, cookies):
        cookies_text = cookie_string(cookies)
        bundle = {
            "cookies": cookies,
            "cookie_string": cookies_text,
            "fingerprint_env": self.fingerprint_env,
            "requests_headers": {
                "Cookie": cookies_text,
            },
        }
        if self.base_headers:
            bundle["base_headers"] = dict(self.base_headers)
        return bundle


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    login = Login()
    result = login.get_qrcode_and_wait_for_login()
    logger.info(
        "login completed cookies_count=%s has_base_headers=%s",
        len(result.get("cookies") or {}),
        bool(result.get("base_headers")),
    )
