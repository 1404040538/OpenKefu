# -*- coding: utf-8 -*-
"""千牛 impaas 长连接客户端（对标 PDD 的 TitanWebSocketClient）。

链路（2026-09 逆向，全链路实测）：
  web 会话 → mtop.taobao.login.token.get.h5 铸 token（imAppKey=千牛 8d61cc42...）
  → wss://wss-cntaobao.dingtalk.com/ JSON LWP 帧：
     /reg 握手（did 必须与铸 token 的 deviceId 完全一致）
     /! 心跳（15s）  /r/<Service>/<fn> RPC  /s/para 实时推送（含 cid）
推送处理：/s/para → 解出 cid → listUserMessages 增量拉取 → on_message 回调。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid as uuid_lib
from typing import Any, Callable

import requests
import websocket

from openkefu.platforms.qianniu.chat.models import (
    ImpaasConversation,
    ImpaasMessage,
    encode_custom_image,
    parse_conversation,
    parse_message,
)
from openkefu.platforms.qianniu.config import (
    DEFAULT_USER_AGENT,
    MTOP_H5_API,
    MTOP_H5_APPKEY,
)

logger = logging.getLogger(__name__)

IMPAAS_WSS_URL = "wss://wss-cntaobao.dingtalk.com/"
QN_IM_APPKEY = "8d61cc42f808efef5d0b87a4a044065e"
IM_TOKEN_API = "mtop.taobao.login.token.get.h5"
IM_TOKEN_VERSION = "2.0"
UA_IM = (DEFAULT_USER_AGENT + " DingTalk(2.1.5) OS(Windows/10) "
         "Browser(Chrome/147.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5")

# ampmedia 图片上传（chat-core bundle 模块 3845 逆向 2026-10-08）：
# web 会话 multipart 直传，appkey=ampmedia，响应 {success, object:{fileId,url,size,pix,fileName}}
AMPMEDIA_UPLOAD_URL = "https://stream-upload.taobao.com/api/upload.api"
AMPMEDIA_APPKEY = "ampmedia"
AMPMEDIA_MAX_BYTES = 10 * 1024 * 1024

CID_RE = re.compile(r"\d+\.1-\d+\.1#\d+@cntaobao")


def new_device_id() -> str:
    return "".join(random.choices("0123456789abcdef", k=32)) + "-" + str(int(time.time() * 1000))


def mint_im_token(session: requests.Session, device_id: str,
                  im_appkey: str = QN_IM_APPKEY, timeout: float = 20) -> dict:
    """用 web 会话铸 impaas token（两步 _m_h5_tk 引导）。

    注意：device_id 必须与后续 /reg 的 did 完全一致。
    返回 {"accessToken": ..., "refreshToken": ...}。
    """
    data = json.dumps({"domain": "cntaobao", "deviceId": device_id,
                       "locale": "zh_CN", "imAppKey": im_appkey}, separators=(",", ":"))

    def _drop_stale_h5tk() -> None:
        """清掉过期的 _m_h5_tk（其值形如 <token>_<expiry_ms>），强制服务端重发。"""
        now_ms = int(time.time() * 1000)
        for cookie in list(session.cookies):
            if cookie.name not in ("_m_h5_tk", "_m_h5_tk_enc"):
                continue
            expired = cookie.expires is not None and cookie.expires * 1000 <= now_ms
            embedded = str(cookie.value or "").split("_")
            if expired or (cookie.name == "_m_h5_tk" and len(embedded) >= 2
                           and embedded[-1].isdigit() and int(embedded[-1]) <= now_ms):
                session.cookies.clear(cookie.domain, cookie.path, cookie.name)

    _drop_stale_h5tk()
    last_ret = ""
    for attempt in range(3):
        token = ""
        for cookie in session.cookies:
            if cookie.name == "_m_h5_tk":
                token = cookie.value.split("_")[0]
                break
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{token}&{t}&{MTOP_H5_APPKEY}&{data}".encode()).hexdigest()
        response = session.get(
            MTOP_H5_API.format(api=IM_TOKEN_API, version=IM_TOKEN_VERSION),
            params={"jsv": "2.7.0", "appKey": MTOP_H5_APPKEY, "t": t, "sign": sign,
                    "api": IM_TOKEN_API, "v": IM_TOKEN_VERSION, "type": "jsonp",
                    "dataType": "jsonp", "callback": "mtopjsonp1", "data": data},
            headers={"Referer": "https://market.m.taobao.com/"}, timeout=timeout)
        match = re.search(r"mtopjsonp1\((\{.*\})\)", response.text, re.S)
        if not match:
            raise RuntimeError(f"im token bad response: {response.text[:200]}")
        body = json.loads(match.group(1))
        ret = (body.get("ret") or [""])[0]
        if "SUCCESS" in ret:
            return body["data"]["result"]
        last_ret = ret
        if "TOKEN_EMPTY" in ret or "TOKEN_EXPIRED" in ret:
            continue  # 首次调用落下新 _m_h5_tk 后重试
        if "PARAMINVALID" in ret or "PARAM_INVALID" in ret:
            _drop_stale_h5tk()
            # PARAMINVALID 常因 _m_h5_tk 过期：清掉后下一次请求会引导新 token
            continue
        raise RuntimeError(f"im token failed: {ret}")
    raise RuntimeError(f"im token failed after retry: {last_ret}")


class ImpaasClientConfig:
    def __init__(self, *, url: str = IMPAAS_WSS_URL, appkey: str = QN_IM_APPKEY,
                 heartbeat_interval: float = 15.0, recv_timeout: float = 5.0,
                 rpc_timeout: float = 15.0, reconnect_backoff: float = 5.0,
                 history_fetch_count: int = 10, poll_interval: float = 12.0):
        self.url = url
        self.appkey = appkey
        self.heartbeat_interval = heartbeat_interval
        self.recv_timeout = recv_timeout
        self.rpc_timeout = rpc_timeout
        self.reconnect_backoff = reconnect_backoff
        self.history_fetch_count = history_fetch_count
        self.poll_interval = poll_interval


class ImpaasClient:
    """常驻 impaas WSS 客户端：RPC + 推送 + 心跳 + 自动重连。

    回调（均在工作线程执行，注意线程安全）：
      on_message(messages: list[ImpaasMessage])  新到买家消息（已过滤自己）
      on_conversation(cid: str)                  会话变更通知
      on_status(status: str, detail: Any)        connecting/online/offline/error
    """

    def __init__(self, session: requests.Session, config: ImpaasClientConfig | None = None,
                 *, on_message: Callable[[list[ImpaasMessage]], None] | None = None,
                 on_conversation: Callable[[str], None] | None = None,
                 on_status: Callable[[str, Any], None] | None = None):
        self.session = session
        self.config = config or ImpaasClientConfig()
        self.on_message = on_message
        self.on_conversation = on_conversation
        self.on_status = on_status
        self.status = "closed"
        self.my_uid = ""  # 1234567890@cntaobao
        self._ws: websocket.WebSocket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mid = random.randrange(10**12, 10**13 - 1)
        self._mid_lock = threading.Lock()
        self._send_lock = threading.Lock()  # websocket-client 的 send 非线程安全，必须串行化
        self._waiters: dict[str, tuple[threading.Event, dict]] = {}
        self._waiters_lock = threading.Lock()
        self._seen_message_ids: dict[str, float] = {}
        self._conv_modify: dict[str, int] = {}  # cid -> 最近观察到的 modifyTime（轮询基线）
        self._session_start = int(time.time() * 1000)
        self._device_id = new_device_id()
        self._access_token = ""
        # 推送处理线程池：避免在收帧线程内同步等 RPC 响应（会自锁）
        from concurrent.futures import ThreadPoolExecutor
        self._push_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="impaas-push")

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="impaas-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
        self._push_executor.shutdown(wait=False)
        self._close_socket()
        self._set_status("offline", None)

    def _set_status(self, status: str, detail: Any = None) -> None:
        self.status = status
        if self.on_status:
            try:
                self.on_status(status, detail)
            except Exception:
                logger.exception("on_status callback failed")
        logger.info("impaas status=%s detail=%s", status, detail)

    # ---------- 主循环 ----------

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception as exc:
                logger.warning("impaas connect failed: %s", exc)
                self._set_status("error", str(exc))
            if self._stop.is_set():
                break
            self._set_status("reconnecting", None)
            self._stop.wait(self.config.reconnect_backoff)

    def _connect_once(self) -> None:
        # token：复用或重铸
        if not self._access_token:
            result = mint_im_token(self.session, self._device_id, self.config.appkey)
            self._access_token = result["accessToken"]
            logger.info("impaas token minted (did=%s...)", self._device_id[:12])

        self._set_status("connecting", None)
        ws = websocket.create_connection(
            self.config.url, timeout=self.config.recv_timeout,
            origin="https://market.m.taobao.com")
        self._ws = ws
        try:
            reg_mid = self._send_frame({"lwp": "/reg", "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": self.config.appkey,
                "token": self._access_token,
                "ua": UA_IM, "dt": "j", "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;", "did": self._device_id,
                "mid": self._next_mid(),
            }})
            # 独立心跳线程：服务端要求 /reg 后 ~15s 内收到心跳，迟到即被
            # /push/kickout 踢线（不能依赖接收循环的 5s 节拍，会输掉竞速）
            hb_stop = threading.Event()

            def _heartbeat_loop() -> None:
                while not hb_stop.wait(self.config.heartbeat_interval * 0.8):
                    if self._stop.is_set():
                        return
                    try:
                        self._send_frame({"lwp": "/!", "headers": {"mid": self._next_mid()}})
                    except Exception:
                        return

            threading.Thread(target=_heartbeat_loop, name="impaas-heartbeat", daemon=True).start()
            last_poll = time.time()
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    now = time.time()
                    if now - last_poll >= self.config.poll_interval:
                        last_poll = now
                        # 轮询兜底：买家消息推送可能被手机端会话抢槽吞掉
                        # （推送唤醒手机淘宝的卖家会话→互踢→积压被手机端消费），
                        # 靠 modifyTime 变化发现新消息，不依赖推送
                        try:
                            self._push_executor.submit(self._poll_conversations)
                        except RuntimeError:
                            pass
                    if raw:
                        self._handle_frame(raw, reg_mid)
                        if self.status != "online" and self.my_uid:
                            self._set_status("online", self.my_uid)
            finally:
                hb_stop.set()
        finally:
            self._close_socket()
            with self._waiters_lock:
                for event, _ in self._waiters.values():
                    event.set()
                self._waiters.clear()
            self._access_token = ""  # 断线后重铸

    def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    # ---------- 帧处理 ----------

    def _handle_frame(self, raw: str, reg_mid: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            logger.debug("impaas raw frame: %s", str(raw)[:120])
            return
        headers = frame.get("headers", {})
        mid = str(headers.get("mid", "")).split(" ")[0]
        logger.debug("impaas frame recv uri=%s mid=%s code=%s len=%d",
                     frame.get("lwp", "-"), mid, frame.get("code"), len(raw))

        if headers.get("reg-sid"):
            self.my_uid = str(headers.get("reg-uid", ""))
            self._resolve_waiter(reg_mid, frame)
            # 异步对齐同步游标（响应到达后由 pts 分支 ackDiff）
            try:
                self._send_frame({"lwp": "/r/SyncStatus/getState",
                                  "headers": {"mid": self._next_mid()},
                                  "body": [{"topic": "sync"}]})
            except Exception:
                logger.exception("send getState failed")
            return
        if headers.get("server-timestamp"):
            return  # 心跳回包
        if frame.get("code") is not None:
            body = frame.get("body")
            if isinstance(body, dict) and "pts" in body and "pipeline" in body:
                # 同步状态 → ack 对齐
                self._send_frame({"lwp": "/r/SyncStatus/ackDiff",
                                  "headers": {"mid": self._next_mid()}, "body": [body]})
            if mid:
                self._resolve_waiter(mid, frame)
            return
        # 推送（无 code，带 mid = 服务端发起的 LWP 请求，必须应答）。
        # 应答格式按真实 SDK 抓包：{code:200, headers:{app-key, mid(完整含" 0"), ua}, body:{}}
        # ——只回显关联三项，多带 sid/dt 会被服务端断连
        uri = frame.get("lwp", "")
        if uri in ("/s/sync", "/s/para", "/s/session/remove"):
            ack_headers = {k: headers[k] for k in ("app-key", "mid", "ua") if k in headers}
            try:
                self._send_frame({"code": 200, "headers": ack_headers, "body": {}})
            except Exception:
                logger.exception("push ack failed")
            self._handle_push(frame)

    def _handle_push(self, frame: dict) -> None:
        body = frame.get("body") or {}
        package = body.get("syncPushPackage") or {}
        cids: list[str] = []
        for item in package.get("data", []) or []:
            blob = item.get("data", "")
            try:
                import base64
                decoded = base64.b64decode(blob).decode("utf-8", errors="ignore")
            except Exception:
                continue
            cids.extend(CID_RE.findall(decoded))
        if not cids:
            return
        for cid in dict.fromkeys(cids):
            logger.info("impaas push cid=%s", cid)
            if self.on_conversation:
                try:
                    self.on_conversation(cid)
                except Exception:
                    logger.exception("on_conversation callback failed")
            # 必须在收帧线程外处理：rpc 同步等待响应会阻塞本线程
            try:
                self._push_executor.submit(self._fetch_new_messages, cid)
            except RuntimeError:
                pass  # 已 shutdown

    def _poll_conversations(self) -> None:
        """轮询会话 modifyTime，发现变化即拉取新消息（推送兜底）。"""
        try:
            response = self.rpc("/r/Conversation/listNewestPagination",
                                [9007199254740991, 10],
                                timeout=self.config.poll_interval * 0.8)
        except Exception:
            return  # 连接切换窗口内的轮询失败直接放弃，下轮再查
        body = response.get("body") or {}
        for uc in (body.get("userConvs") or []):
            sc = uc.get("singleChatUserConversation") or {}
            cid = str((sc.get("singleChatConversation") or {}).get("cid") or "")
            modify = int(sc.get("modifyTime") or 0)
            if not cid:
                continue
            known = self._conv_modify.get(cid)
            if known is None:
                # 未知会话：早于本次进程启动的只记基线；启动后活跃的立即拉取
                self._conv_modify[cid] = modify
                if modify >= self._session_start:
                    self._fetch_new_messages(cid)
            elif modify > known:
                self._conv_modify[cid] = modify
                self._fetch_new_messages(cid)

    def _fetch_new_messages(self, cid: str) -> None:
        """增量拉取会话新消息并回调。"""
        try:
            cursor = int(time.time() * 1000) + 60000
            response = self.rpc("/r/MessageManager/listUserMessages",
                                [cid, False, cursor, self.config.history_fetch_count, True])
        except Exception:
            logger.exception("listUserMessages failed cid=%s", cid)
            return
        body = (response or {}).get("body") or {}
        models = body.get("userMessageModels") if isinstance(body, dict) else None
        if not models:
            return
        now = time.time()
        fresh: list[ImpaasMessage] = []
        for model in models:
            message = parse_message(model)
            if not message:
                continue
            if message.message_id in self._seen_message_ids:
                continue
            self._seen_message_ids[message.message_id] = now
            if self.my_uid and message.sender_uid == self.my_uid:
                continue  # 自己发的
            if message.content_type in (0,):
                continue
            # 时间兜底：跳过早于本次进程启动的消息（防历史回放），
            # 启动后任何时点（含断线窗口）的消息都放行
            if message.create_at and message.create_at < self._session_start - 60_000:
                continue
            fresh.append(message)
        # 清理过期去重键（保留 1 小时）
        if len(self._seen_message_ids) > 5000:
            cutoff = now - 3600
            self._seen_message_ids = {k: v for k, v in self._seen_message_ids.items() if v > cutoff}
        if fresh and self.on_message:
            try:
                self.on_message(fresh)
            except Exception:
                logger.exception("on_message callback failed")

    # ---------- RPC ----------

    def _next_mid(self) -> str:
        with self._mid_lock:
            self._mid += 1
            return f"{self._mid} 0"

    def _send_frame(self, frame: dict) -> str:
        ws = self._ws
        if ws is None:
            raise RuntimeError("impaas socket not connected")
        payload = json.dumps(frame)
        logger.debug("impaas frame send uri=%s mid=%s code=%s len=%d",
                     frame.get("lwp", "-"),
                     str(frame.get("headers", {}).get("mid", "-")),
                     frame.get("code"), len(payload))
        with self._send_lock:
            ws.send(payload)
        return str(frame.get("headers", {}).get("mid", "")).split(" ")[0]

    def _resolve_waiter(self, mid: str, frame: dict) -> None:
        with self._waiters_lock:
            waiter = self._waiters.pop(mid, None)
        if waiter:
            waiter[1].update(frame)
            waiter[0].set()

    def rpc(self, uri: str, body: Any, *, timeout: float | None = None) -> dict:
        """同步 RPC：发送帧并等待同 mid 响应。"""
        timeout = timeout or self.config.rpc_timeout
        mid = self._send_frame({"lwp": uri, "headers": {"mid": self._next_mid()}, "body": body})
        event = threading.Event()
        slot: dict = {}
        with self._waiters_lock:
            self._waiters[mid] = (event, slot)
        if not event.wait(timeout):
            with self._waiters_lock:
                self._waiters.pop(mid, None)
            raise TimeoutError(f"impaas rpc timeout: {uri} mid={mid}")
        code = slot.get("code")
        if code not in (200, None):
            raise RuntimeError(f"impaas rpc failed: {uri} code={code} body={json.dumps(slot.get('body'), ensure_ascii=False)[:200]}")
        return slot

    def _sync_state(self) -> bool:
        """从其他线程调用的同步游标对齐（收帧线程内勿用，会自锁）。"""
        try:
            response = self.rpc("/r/SyncStatus/getState", [{"topic": "sync"}])
            body = response.get("body") or {}
            if isinstance(body, dict) and "pts" in body:
                self.rpc("/r/SyncStatus/ackDiff", [body])
                return True
        except Exception:
            logger.exception("sync state failed")
        return False

    # ---------- 业务 API ----------

    def list_conversations(self, *, count: int = 20) -> list[ImpaasConversation]:
        response = self.rpc("/r/Conversation/listNewestPagination", [9007199254740991, count])
        body = response.get("body") or {}
        convs = []
        for uc in (body.get("userConvs") or []):
            conv = parse_conversation(uc)
            if conv:
                convs.append(conv)
        return convs

    def list_messages(self, cid: str, *, count: int = 20) -> list[ImpaasMessage]:
        """拉取会话最新消息（时间倒序）。

        listUserMessages 的 cursor 是 createdAt 毫秒值：
        forward=False + cursor=<now> → 从最新往回取 N 条（本方法用法）；
        forward=True + cursor=0 会从最旧向后取（勿用）。
        """
        cursor = int(time.time() * 1000) + 60000
        response = self.rpc("/r/MessageManager/listUserMessages", [cid, False, cursor, count, True])
        body = response.get("body") or {}
        messages = []
        for model in (body.get("userMessageModels") or []):
            message = parse_message(model)
            if message:
                messages.append(message)
        return messages

    def send_text(self, cid: str, receiver_uid_num: str, text: str) -> dict:
        """发送文本消息。receiver_uid_num 为对方数字 uid（买家）。"""
        model = {
            "uuid": str(uuid_lib.uuid4()),
            "cid": cid,
            "conversationType": 1,
            "content": {"contentType": 1, "text": {"content": text, "extension": {}}},
            "redPointPolicy": 1, "extension": {}, "ctx": {},
            "mtags": {}, "msgReadStatusSetting": 1,
        }
        response = self.rpc("/r/MessageSend/sendByReceiverScope",
                            [model, {"actualReceivers": [f"{receiver_uid_num}@cntaobao"]}])
        body = response.get("body") or {}
        if not body.get("messageId"):
            raise RuntimeError(f"send_text no messageId: {json.dumps(body, ensure_ascii=False)[:200]}")
        return body

    # ---------- 图片消息（ampmedia 上传 + custom IMAGE 编码） ----------

    def upload_image(self, image_bytes: bytes, filename: str = "image.jpg") -> dict:
        """ampmedia 上传图片，返回 {fileId, url, size, width, height, suffix}。

        走 web 会话 cookie（requests.Session），协议对齐 chat-core 模块 3845。
        """
        if len(image_bytes) > AMPMEDIA_MAX_BYTES:
            raise ValueError("图片大小不能超过 10MB")
        response = self.session.post(
            AMPMEDIA_UPLOAD_URL,
            params={"appkey": AMPMEDIA_APPKEY, "folderId": "0", "_input_charset": "utf-8"},
            files={"file": (filename, image_bytes, "image/jpeg")},
            headers={"Referer": "https://market.m.taobao.com/"},
            timeout=60,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"ampmedia upload non-json response: {response.text[:200]}") from exc
        obj = body.get("object") or {}
        if not body.get("success") or not obj.get("url"):
            raise RuntimeError(f"ampmedia upload failed: {json.dumps(body, ensure_ascii=False)[:300]}")
        width, height = 0, 0
        pix = str(obj.get("pix") or "")
        if "x" in pix:
            parts = pix.split("x", 1)
            if parts[0].isdigit() and parts[1].isdigit():
                width, height = int(parts[0]), int(parts[1])
        url = str(obj["url"])
        return {
            "fileId": str(obj.get("fileId") or ""),
            "url": url,
            "size": int(obj.get("size") or 0),
            "width": width,
            "height": height,
            "suffix": url.rsplit(".", 1)[-1].lower() if "." in url else "jpg",
        }

    def send_image(self, cid: str, receiver_uid_num: str, image_bytes: bytes,
                   filename: str = "image.jpg") -> tuple[dict, str]:
        """发送图片消息：ampmedia 上传后以 custom(type=7) 编码发送。

        返回 (send 响应 body, 图片 URL)。对齐 web 端 chat-core 行为，
        买家 web / 移动端均可渲染。
        """
        meta = self.upload_image(image_bytes, filename=filename)
        model = {
            "uuid": str(uuid_lib.uuid4()),
            "cid": cid,
            "conversationType": 1,
            "content": encode_custom_image(meta),
            "redPointPolicy": 1, "extension": {}, "ctx": {},
            "mtags": {}, "msgReadStatusSetting": 1,
        }
        response = self.rpc("/r/MessageSend/sendByReceiverScope",
                            [model, {"actualReceivers": [f"{receiver_uid_num}@cntaobao"]}])
        body = response.get("body") or {}
        if not body.get("messageId"):
            raise RuntimeError(f"send_image no messageId: {json.dumps(body, ensure_ascii=False)[:200]}")
        return body, meta["url"]
