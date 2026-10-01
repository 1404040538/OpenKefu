# -*- coding: utf-8 -*-
"""千牛（淘宝商家）扫码登录。

2026-09 逆向结论（对齐千牛 PC 客户端 AliWorkbench 9.97 的登录链路）：

- 千牛 PC 登录窗口在 CEF webview 里加载
  ``login.taobao.com/member/qrcode.htm?from=pcsdk&qrversion=2017&appkey=24585574``。
- pcsdk 模式轮询成功（code=10006）只返回 lgToken，由原生 AliAuthSDK 走 mtop
  ``winpc.taobao.havana.mlogin.qrcodelogin`` 换取客户端会话（acs 网关 + native 签名，未复刻）。
- 本模块走 Web 等价链路：同端点 ``from=site`` 模式轮询成功后返回
  ``login.taobao.com/member/loginByIm.do?...&token=<lgToken>&webpas=...``，
  直接 POST ``/newlogin/token/loginByIm.do`` 即可在响应中 Set-Cookie 全套
  web 登录态（unb/skt/sg/cookie1/uc1/uc3/sgcookie/lid/lc/cookie2/_tb_token_ 等），
  该会话可访问千牛工作台 qn.taobao.com 与 h5api mtop（_m_h5_tk 签名）。

轮询状态码（qrcode.htm 内联 initData.codeMapObj）：
  10000 等待扫码 / 10001 已扫码待确认 / 10006 登录成功 /
  10004 二维码过期 / 10002,10005 系统异常
"""

import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlparse

import requests

from openkefu.platforms.qianniu.common import parse_jsonp, session_cookie_dict
from openkefu.platforms.qianniu.config import (
    DEFAULT_USER_AGENT,
    LOGIN_COMPLETE_URL,
    MTOP_H5_API,
    MTOP_H5_APPKEY,
    QN_HOME_URL,
    QR_CHECK_URL,
    QR_GENERATE_URL,
    QR_PAGE_URL,
    QRCODE_DIR,
)


logger = logging.getLogger(__name__)

STATUS_WAITING = "10000"
STATUS_SCANNED = "10001"
STATUS_SUCCESS = "10006"
STATUS_EXPIRED = "10004"
STATUS_SYSTEM_ERRORS = ("10002", "10005")

# 登录态核心 cookie（会话有效性判断与后续业务使用）。
SESSION_COOKIE_NAMES = (
    "unb", "skt", "sg", "cookie1", "cookie2", "cookie17", "sgcookie",
    "uc1", "uc3", "uc4", "lgc", "dnk", "tracknick", "_nk_", "lid", "lc",
    "_l_g_", "existShop", "cancelledSubSites", "csg", "_tb_token_", "t",
)


class QrcodeExpiredError(RuntimeError):
    """二维码已失效（10004），需重新生成。"""


class LoginSystemError(RuntimeError):
    """登录系统异常（10002/10005），可稍后重试。"""


class RiskControlError(RuntimeError):
    """触发淘宝 x5sec 风控（punish 页），需更换出口 IP 或降低频率后重试。"""


def _is_punished(response) -> bool:
    if "_____tmd_____/" in response.url or "x5sec" in response.url:
        return True
    return "_____tmd_____/punish" in (response.text or "")


class Login:
    """千牛扫码登录（纯协议，无浏览器依赖）。"""

    def __init__(self, qrcode_dir=None, timeout=15):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.timeout = timeout
        self.qrcode_dir = QRCODE_DIR if qrcode_dir is None else qrcode_dir
        self.lg_token = None
        self.ad_token = None
        self.qr_image_url = None
        self.qr_image_file = None
        self.login_url = None
        self.nick = None
        self.user_id = None

    # ---------- 基础请求 ----------

    def _get(self, url, *, params=None, referer="https://login.taobao.com/", **kwargs):
        response = self.session.get(
            url, params=params, timeout=self.timeout,
            headers={"Referer": referer, **kwargs.pop("headers", {})}, **kwargs)
        if _is_punished(response):
            raise RiskControlError(f"x5sec punish: {response.url[:200]}")
        return response

    # ---------- 登录前置 ----------

    def init_login_cookies(self) -> dict:
        """打开扫码页，获取 cookie2 / t / _tb_token_ / XSRF-TOKEN 等前置 cookie。"""
        response = self._get(QR_PAGE_URL)
        if response.status_code != 200:
            raise LoginSystemError(f"qrcode page status={response.status_code}")
        cookies = session_cookie_dict(self.session)
        missing = [name for name in ("cookie2", "t", "_tb_token_") if name not in cookies]
        if missing:
            raise LoginSystemError(f"bootstrap cookies missing: {missing}")
        return cookies

    # ---------- 二维码 ----------

    def get_qrcode(self, *, retry_count=3) -> dict:
        """生成登录二维码并保存 PNG，返回 {lgToken, adToken, qrImageFile, qrImageUrl}。"""
        last_error = None
        for _ in range(max(1, retry_count)):
            response = self._get(
                QR_GENERATE_URL,
                params={"_": int(time.time() * 1000), "callback": "jsonp1"})
            try:
                data = parse_jsonp(response.text)
            except ValueError as exc:
                last_error = exc
                continue
            if not data.get("success"):
                last_error = LoginSystemError(f"generate failed: {data}")
                continue
            self.lg_token = data.get("lgToken")
            self.ad_token = data.get("adToken")
            self.qr_image_url = data.get("url") or ""
            self._download_qr_image()
            logger.info("qrcode generated lgToken=%s", self.lg_token)
            return {
                "lgToken": self.lg_token,
                "adToken": self.ad_token,
                "qrImageFile": str(self.qr_image_file) if self.qr_image_file else None,
                "qrImageUrl": self.qr_image_url,
            }
        raise last_error or LoginSystemError("generate failed")

    def _download_qr_image(self):
        if not self.qr_image_url:
            return
        url = self.qr_image_url
        if url.startswith("//"):
            url = "https:" + url
        response = self._get(url, referer="https://login.taobao.com/")
        response.raise_for_status()
        self.qrcode_dir.mkdir(parents=True, exist_ok=True)
        self.qr_image_file = self.qrcode_dir / f"qianniu_{self.lg_token}.png"
        self.qr_image_file.write_bytes(response.content)

    # ---------- 轮询 ----------

    def query_qrcode(self, token=None) -> dict:
        """轮询一次二维码状态，返回 {code, message, success, url?...}。"""
        token = token or self.lg_token
        if not token:
            raise ValueError("lgToken is empty; call get_qrcode() first")
        response = self._get(
            QR_CHECK_URL,
            params={"lgToken": token, "t": 0, "lt": 0,
                    "_": int(time.time() * 1000), "callback": "jsonp2"})
        data = parse_jsonp(response.text)
        code = str(data.get("code", ""))
        if code == STATUS_EXPIRED:
            raise QrcodeExpiredError(data.get("message") or "qrcode expired")
        if code in STATUS_SYSTEM_ERRORS:
            raise LoginSystemError(f"{code}: {data.get('message', '')}")
        return data

    def wait_for_login(self, token=None, *, poll_interval=2, timeout=180, on_status=None) -> dict:
        """轮询直到登录成功（10006），返回携带 loginByIm.do 换会话 URL 的响应。"""
        token = token or self.lg_token
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.query_qrcode(token)
            code = str(data.get("code", ""))
            if on_status:
                on_status(code, data)
            if code == STATUS_SUCCESS:
                return data
            time.sleep(poll_interval)
        raise TimeoutError(f"qrcode login timed out after {timeout}s")

    # ---------- 换会话 ----------

    def complete_login(self, login_url=None) -> dict:
        """POST /newlogin/token/loginByIm.do，落全套 web 会话 cookie。

        实测无需 baxia（bx-ua/bx-umidtoken/bx_et）参数即可成功；若后续风控收紧，
        可在此补充这三项（浏览器抓包 login-with-baxia-et 流程）。
        """
        login_url = login_url or self.login_url
        if not login_url:
            raise ValueError("login_url is empty; poll must return it first")
        params = dict(parse_qsl(urlparse(login_url).query))
        form = {
            "webpas": params.get("webpas", ""),
            "uid": params.get("uid", ""),
            "ask_version": params.get("ask_version", "1.0.0"),
            "asker": params.get("asker", "qrcodelogin"),
            "time": params.get("time", ""),
            "defaulturl": params.get("defaulturl", ""),
            "token": params.get("token", ""),
        }
        response = self.session.post(
            LOGIN_COMPLETE_URL, data=form, timeout=self.timeout,
            headers={"Referer": login_url, "Origin": "https://login.taobao.com",
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded"})
        if _is_punished(response):
            raise RiskControlError(f"x5sec punish: {response.url[:200]}")
        body = response.json()
        data = (body.get("content") or {}).get("data") or {}
        if not body.get("success") or data.get("resultCode") not in (100, None):
            raise LoginSystemError(f"complete_login failed: {json.dumps(body, ensure_ascii=False)[:300]}")
        self.nick = (params.get("uid") or "").removeprefix("cntaobao")
        self.user_id = session_cookie_dict(self.session).get("unb")
        logger.info("login completed nick=%s unb=%s", self.nick, self.user_id)
        return data

    # ---------- 组合入口 ----------

    def get_qrcode_and_wait_for_login(self, *, poll_interval=2, timeout=180, on_status=None) -> dict:
        """一键流程：初始化 → 出码 → 轮询 → 换会话。"""
        self.init_login_cookies()
        qr = self.get_qrcode()
        result = self.wait_for_login(qr["lgToken"], poll_interval=poll_interval,
                                     timeout=timeout, on_status=on_status)
        self.login_url = result.get("url") or result.get("iframeRedirectUrl")
        if self.login_url and not self.login_url.startswith("http"):
            self.login_url = "https:" + self.login_url
        complete = self.complete_login(self.login_url)
        return {
            "lgToken": qr["lgToken"],
            "qrImageFile": qr["qrImageFile"],
            "nick": self.nick,
            "userId": self.user_id,
            "complete": complete,
        }

    # ---------- 会话访问 ----------

    def get_target_cookies(self) -> dict:
        """返回登录态 cookie（核心项优先，含全部）。"""
        cookies = session_cookie_dict(self.session)
        return {name: cookies[name] for name in SESSION_COOKIE_NAMES if name in cookies}

    def user_info(self):
        """mtop.user.getUserSimple 验证会话（两步 _m_h5_tk 引导 + md5 签名）。

        返回 mtop data（含 nick/userId），失败返回 None。
        """
        api, version = "mtop.user.getUserSimple", "1.0"
        data_str = "{}"
        for _ in range(2):
            token = ""
            for cookie in self.session.cookies:
                if cookie.name == "_m_h5_tk":
                    token = cookie.value.split("_")[0]
                    break
            timestamp = str(int(time.time() * 1000))
            sign = hashlib.md5(
                f"{token}&{timestamp}&{MTOP_H5_APPKEY}&{data_str}".encode()).hexdigest()
            response = self._get(
                MTOP_H5_API.format(api=api, version=version),
                params={
                    "jsv": "2.7.2", "appKey": MTOP_H5_APPKEY, "t": timestamp,
                    "sign": sign, "api": api, "v": version,
                    "type": "originaljson", "dataType": "json", "timeout": "20000",
                    "data": data_str,
                },
                referer=QN_HOME_URL)
            try:
                body = response.json()
            except ValueError:
                return None
            ret = body.get("ret", [])
            if any(r.startswith("SUCCESS") for r in ret):
                return body.get("data")
            if any("TOKEN_EMPTY" in r or "TOKEN_EXPIRED" in r for r in ret):
                continue  # 首次调用落下 _m_h5_tk 后重试
            return None
        return None
