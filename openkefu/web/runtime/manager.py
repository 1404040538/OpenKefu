from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import qrcode as qrcode_lib
from pymysql.err import IntegrityError

from openkefu.platforms.pdd.auth.login import CaptchaRequiredError, Login
from openkefu.platforms.pdd.chat.auto_reply import (
    incoming_chat_messages,
    incoming_display_messages,
    is_system_notice_message,
    message_dedupe_key,
    user_message_summary,
)
from openkefu.platforms.pdd.chat.actions import ActionDispatcher
from openkefu.platforms.pdd.chat.conversation import build_conversation_messages
from openkefu.platforms.pdd.chat.customer_service import CustomerServiceClient
from openkefu.platforms.pdd.chat.customer_transfer import CustomerTransferClient
from openkefu.platforms.pdd.chat.fast_reply import detect_fast_reply
from openkefu.platforms.pdd.chat.goods import GoodsService
from openkefu.platforms.pdd.chat.intent import clean_reply_text
from openkefu.platforms.pdd.chat.knowledge import KnowledgeService, NoteSetService
from openkefu.platforms.pdd.chat.llm import LLMClient, get_llm_client
from openkefu.platforms.pdd.chat.orders import OrderService
from openkefu.platforms.pdd.chat.reply_cache import ReplyCache
from openkefu.services.embedding import build_embed_fn
from openkefu.platforms.pdd.chat.return_records import ReturnRecordService
from openkefu.platforms.pdd.chat.send_policy import (
    is_session_expired,
    send_message_failure_text,
    send_message_response_ok,
)
from openkefu.platforms.pdd.config import QRCODE_DIR
from openkefu.web.config import AppConfig
from openkefu.web.crypto import TextCipher
from openkefu.web.db import Database, json_dumps
from openkefu.web.ratelimit import FixedWindowRateLimiter
from openkefu.web.realtime import RealtimeHub, RuntimeLogger
from openkefu.web.runtime_bus import RuntimeCommandBus

from openkefu.web.runtime.shop_runner import PasswordVerificationRequired, ShopRunner


def _worker_local_pid(worker_id: str) -> int | None:
    """从默认 worker_id（<hostname>-<pid>-<hex>）解出本机 pid；格式不符或
    跨机 worker 返回 None。hostname 自身可含 '-'，从右侧固定取两段。"""
    if not worker_id:
        return None
    parts = worker_id.rsplit("-", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    hostname = parts[0]
    if hostname != socket.gethostname():
        return None
    return int(parts[1])


def _is_local_pid_alive(pid: int) -> bool:
    """本机 pid 存活检查；Windows 下同时校验映像名以规避 pid 复用误判。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            size = ctypes.c_ulong(512)
            buf = ctypes.create_unicode_buffer(512)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                image = (buf.value or "").lower()
                if "python" not in image:
                    return False  # pid 被无关进程复用
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class ShopRuntimeManager:
    STARTUP_RECONNECT_GRACE_SECONDS = 15
    # recovery 接管缓冲：必须远小于 stale cleanup 的 grace，
    # 保证崩溃店铺先被 recovery 接管而不是被标 offline
    STARTUP_TAKEOVER_BUFFER_SECONDS = 2

    def __init__(self, db: Database, hub: RealtimeHub, runtime_logger: RuntimeLogger, config: AppConfig):
        self.db = db
        self.hub = hub
        self.runtime_logger = runtime_logger
        self.config = config
        self._runners: dict[int, ShopRunner] = {}
        self._lock = threading.Lock()
        self._llm_client: LLMClient | None = None
        self._reply_cache: ReplyCache | None = None
        default_worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.worker_id = config.runtime.worker_id or default_worker_id
        self._owned_shop_ids: set[int] = set()
        self._stop_event = threading.Event()
        self._lease_thread: threading.Thread | None = None
        self._daily_reconnect_thread: threading.Thread | None = None
        self._startup_reconnect_thread: threading.Thread | None = None
        self._stale_cleanup_thread: threading.Thread | None = None
        self._data_cleanup_thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self.reply_executor = ThreadPoolExecutor(
            max_workers=config.runtime.reply_workers,
            thread_name_prefix="reply-worker",
        )
        from openkefu.web.repositories import Repositories
        self.repos = Repositories(db)
        self.command_bus = RuntimeCommandBus(config.redis.url, config.runtime.command_secret)
        limiter_enabled = not config.security.rate_limit_disabled
        self._sms_limiter = FixedWindowRateLimiter(max_hits=1, window_seconds=60, enabled=limiter_enabled)
        self._password_login_limiter = FixedWindowRateLimiter(max_hits=10, window_seconds=600, enabled=limiter_enabled)

    @property
    def llm_client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client(self.config.llm)
        return self._llm_client

    @property
    def reply_cache(self) -> ReplyCache:
        if self._reply_cache is None:
            # 语义相似匹配必须走 embedding 配置的供应商，而非 LLM 客户端。
            self._reply_cache = ReplyCache(build_embed_fn(self.config.embedding))
        return self._reply_cache

    def runner(self, shop_id: int) -> ShopRunner:
        shop_id = int(shop_id)
        with self._lock:
            if shop_id not in self._runners:
                row = self.db.query_one("SELECT platform FROM shops WHERE id=%s", (shop_id,))
                platform = (row or {}).get("platform") or "pdd"
                if platform == "qianniu":
                    from openkefu.platforms.qianniu.runtime import QianniuShopRunner
                    self._runners[shop_id] = QianniuShopRunner(
                        shop_id, self.db, self.hub, self.runtime_logger, self.config,
                        llm_client=self.llm_client,
                        reply_cache=self.reply_cache,
                        manager=self,
                    )
                else:
                    self._runners[shop_id] = ShopRunner(
                        shop_id, self.db, self.hub, self.runtime_logger, self.config,
                        llm_client=self.llm_client,
                        reply_cache=self.reply_cache,
                        manager=self,
                    )
            return self._runners[shop_id]

    def start_shop(self, shop_id: int) -> dict[str, Any]:
        return self.login_shop(shop_id)

    def login_shop(self, shop_id: int) -> dict[str, Any]:
        self.acquire_shop_lease(shop_id, required=True)
        return self.runner(shop_id).login_shop()

    def password_login_shop(self, shop_id: int, username: str, password: str, verify_code: str = "") -> dict[str, Any]:
        if verify_code:
            if not self._password_login_limiter.hit(f"verify:{shop_id}"):
                raise RuntimeError("验证尝试过于频繁，请稍后再试")
        else:
            if not self._password_login_limiter.hit(f"login:{shop_id}:{username}"):
                raise RuntimeError("登录尝试过于频繁，请稍后再试")
        self.acquire_shop_lease(shop_id, required=True)
        return self.runner(shop_id).password_login_shop(username, password, verify_code)

    def password_login_send_sms(self, shop_id: int) -> bool:
        if not self._sms_limiter.hit(f"sms:{shop_id}"):
            raise RuntimeError("短信发送过于频繁，请 60 秒后再试")
        runner = self.runner(shop_id)
        state = getattr(runner, "_password_login_state", None) or {}
        if not state:
            raise RuntimeError("没有待验证的登录会话")
        if int(state.get("shop_id") or 0) != int(shop_id):
            runner._password_login_state = None
            raise RuntimeError("password login verification state does not match current shop")
        if float(state.get("expires_at") or 0) < time.time():
            runner._password_login_state = None
            runner._set_error(RuntimeError("password login verification expired"))
            raise RuntimeError("password login verification expired")
        login = state.get("login") or runner.login
        if login is None:
            runner._password_login_state = None
            raise RuntimeError("登录验证会话已失效，请重新登录")
        return login.send_mobile_verify_code(state["username"])

    def password_login_verify(self, shop_id: int, verify_code: str) -> dict[str, Any]:
        if not self._password_login_limiter.hit(f"verify:{shop_id}"):
            raise RuntimeError("验证尝试过于频繁，请稍后再试")
        self.acquire_shop_lease(shop_id, required=True)
        runner = self.runner(shop_id)
        return runner._complete_password_login(verify_code)

    def online_shop(self, shop_id: int) -> dict[str, Any]:
        self.acquire_shop_lease(shop_id, required=True)
        return self.runner(shop_id).online()

    def offline_shop(self, shop_id: int) -> None:
        try:
            self.runner(shop_id).offline()
        finally:
            self.release_shop_lease(shop_id, status="offline")

    def stop_shop(self, shop_id: int) -> None:
        # runner 停止失败也必须释放租约——否则遗留 online 租约
        # 会拖住下一次接管一个完整 TTL 周期
        try:
            self.runner(shop_id).stop()
        finally:
            self.release_shop_lease(shop_id, status="stopped")

    def acquire_shop_lease(self, shop_id: int, *, required: bool = False,
                           wait_seconds: float = 95.0) -> bool:
        """获取店铺运行租约；被其他存活 worker 持有时等待其过期后重试。

        wait_seconds：旧租约未过期时的最长等待（默认覆盖一个完整
        lease TTL 周期）。等待期间每 3 秒重试一次；超时仍失败时，
        required=True 抛错、False 返回 False。
        """
        shop_id = int(shop_id)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            acquired = self.repos.shops.acquire_lease(
                shop_id,
                worker_id=self.worker_id,
                pid=os.getpid(),
                ttl_seconds=int(self.config.runtime.lease_ttl_seconds),
                required=False,
            )
            if acquired:
                with self._lock:
                    self._owned_shop_ids.add(shop_id)
                return True
            if not required:
                return False
            row = self.repos.shops.lease_row(shop_id) or {}
            holder = str(row.get("worker_id") or "")
            # 同机另一个存活实例正持有租约且在续期——等待注定超时
            # （多实例并存还会导致千牛同账号互踢），立即失败并给出处置指引
            holder_pid = _worker_local_pid(holder)
            if holder_pid is not None and _is_local_pid_alive(holder_pid):
                raise RuntimeError(
                    f"店铺运行锁由本机另一个服务实例持有（PID {holder_pid}）。"
                    f"多实例并存会导致连接互踢，请先结束它：taskkill /PID {holder_pid} /F，"
                    f"或直接在该实例的网页里操作。"
                )
            remaining = self.repos.shops.lease_remaining_seconds(shop_id)
            if remaining is None or remaining <= 0:
                # 无租约或已过期却仍获取失败（如恰好被并发抢占），短暂等待后重试
                remaining = 3.0
            if time.monotonic() + remaining > deadline:
                raise RuntimeError(
                    f"店铺运行锁被其他实例持有（worker={holder or '?'}），"
                    f"其租约约 {int(remaining)} 秒后过期；请稍后重试"
                )
            self.runtime_logger.log(
                "INFO",
                __name__,
                "runtime.lease.wait",
                "waiting for previous worker lease to expire",
                shop_id=shop_id,
                context={"remaining_seconds": int(remaining), "holder": holder,
                         "worker_id": self.worker_id},
            )
            time.sleep(min(remaining + 1.0, 5.0))

    def shutdown(self) -> None:
        """优雅退出：停止所有 runner、释放租约、标记店铺下线。

        以 _owned_shop_ids（租约权威集合）为准——acquire 过但尚未创建
        runner 的店铺也要释放。由 web lifespan 的 finally 调用；
        kill -9 场景无法触达，依赖 lease TTL 自然过期 + stale cleanup 纠正。
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                return  # lifespan 与 atexit 双路径都会调用，幂等短路
            self._shutdown_done = True
            with self._lock:
                shop_ids = list(self._owned_shop_ids)
                known_runners = list(self._runners.keys())
            for shop_id in shop_ids:
                try:
                    if shop_id in known_runners:
                        self.stop_shop(shop_id)
                    else:
                        # 持有租约但没有 runner（如刚 acquire 完成还没上线）：
                        # 直接释放并纠正店铺状态
                        self.release_shop_lease(shop_id, status="offline")
                        try:
                            self.repos.shops.mark_shop_offline_with_error(
                                shop_id, "runtime worker shutting down")
                        except Exception:
                            pass
                except Exception as exc:
                    self.runtime_logger.log(
                        "ERROR",
                        __name__,
                        "runtime.shutdown.runner",
                        "failed to stop runner during shutdown",
                        shop_id=shop_id,
                        error=exc,
                    )
            self.stop_background_services()
            self.runtime_logger.log(
                "INFO",
                __name__,
                "runtime.shutdown",
                "runtime manager shut down",
                context={"worker_id": self.worker_id, "leased_shops": len(shop_ids)},
            )

    def release_shop_lease(self, shop_id: int, *, status: str) -> None:
        shop_id = int(shop_id)
        self.repos.shops.release_lease(shop_id, worker_id=self.worker_id, status=status)
        with self._lock:
            self._owned_shop_ids.discard(shop_id)

    def owns_shop(self, shop_id: int) -> bool:
        return self.repos.shops.owns_shop(int(shop_id), worker_id=self.worker_id)

    def start_background_services(self) -> None:
        if self._lease_thread and self._lease_thread.is_alive():
            return
        self._stop_event.clear()
        self._lease_thread = threading.Thread(target=self._lease_heartbeat_loop, name="runtime-lease-heartbeat", daemon=True)
        self._lease_thread.start()
        self._daily_reconnect_thread = threading.Thread(target=self._daily_reconnect_loop, name="runtime-daily-reconnect", daemon=True)
        self._daily_reconnect_thread.start()
        self._stale_cleanup_thread = threading.Thread(target=self._stale_online_cleanup_loop, name="runtime-stale-cleanup", daemon=True)
        self._stale_cleanup_thread.start()
        self._data_cleanup_thread = threading.Thread(target=self._data_cleanup_loop, name="runtime-data-cleanup", daemon=True)
        self._data_cleanup_thread.start()
        self._startup_reconnect_thread = threading.Thread(
            target=self.recover_restart_online_shops,
            name="runtime-startup-reconnect",
            daemon=True,
        )
        self._startup_reconnect_thread.start()

    def _data_cleanup_loop(self) -> None:
        """定期清理过期运行数据，防止 runtime_logs / intent_events / 尝试记录无限增长。"""
        while not self._stop_event.wait(3600):
            try:
                self._cleanup_expired_data()
            except Exception as exc:
                self.runtime_logger.log(
                    "ERROR",
                    __name__,
                    "runtime.data_cleanup",
                    "data cleanup failed",
                    error=exc,
                )

    def _cleanup_expired_data(self) -> None:
        now = datetime.now()
        results: dict[str, int] = {}
        # 运行日志保留 30 天
        results["runtime_logs"] = self.repos.logs.delete_old(days=30)
        # 意图/动作/回复/转接等尝试记录保留 90 天
        results.update(self.repos.conversations.cleanup_old_attempts(days=90))
        # 二维码登录尝试保留 7 天（含二维码图片文件清理）
        stale_attempts = self.repos.shops.stale_qr_attempts(days=7)
        for attempt in stale_attempts:
            path = Path(str(attempt.get("qrcode_path") or ""))
            try:
                if path.exists() and path.is_file():
                    path.unlink()
            except OSError:
                pass
        if stale_attempts:
            results["qr_login_attempts"] = self.repos.shops.delete_stale_qr_attempts(days=7)
        self.runtime_logger.log(
            "INFO",
            __name__,
            "runtime.data_cleanup",
            "expired runtime data cleaned",
            context={"cleaned_at": now.isoformat(sep=" ", timespec="seconds"), "deleted": results},
        )

    def stop_background_services(self) -> None:
        self._stop_event.set()
        self.reply_executor.shutdown(wait=False, cancel_futures=True)

    def recover_restart_online_shops(self) -> None:
        # 崩溃残留的租约可能在本进程启动后才过期（TTL 未走完），
        # 一次性扫描会扑空——在接管窗口内循环重扫，直到无候选
        grace = self._startup_reconnect_grace_seconds()
        ttl = int(self.config.runtime.lease_ttl_seconds)
        window = grace + ttl + 30
        deadline = time.monotonic() + window
        handled: set[int] = set()
        while not self._stop_event.is_set():
            candidates = self._startup_reconnect_candidates()
            pending = [int(row["id"]) for row in candidates if int(row["id"]) not in handled]
            for shop_id in pending:
                if self._stop_event.is_set():
                    return
                self._auto_reconnect_shop_after_restart(shop_id)
                handled.add(shop_id)
                self._stop_event.wait(1)
            if time.monotonic() >= deadline:
                break
            # 还有"仍在租约保护期内的 online 店铺"时继续等它过期
            if not self.repos.shops.has_pending_takeover_candidates(
                    worker_id=self.worker_id,
                    grace_seconds=self.STARTUP_TAKEOVER_BUFFER_SECONDS):
                break
            self._stop_event.wait(10)
        self.runtime_logger.log(
            "INFO",
            __name__,
            "runtime.startup_recovery",
            "startup runtime recovery finished",
            context={
                "worker_id": self.worker_id,
                "reconnected": sorted(handled),
                "window_seconds": window,
            },
        )

    def _startup_reconnect_grace_seconds(self) -> int:
        # grace = 租约过期后的额外接管缓冲（防时钟偏差/续租边缘竞态），
        # 总接管等待 ≈ lease TTL + grace。原子性由 claim SQL 的
        # UPDATE ... WHERE expires_at<... 保证，多 worker 并发只有一个成功。
        ttl = int(self.config.runtime.lease_ttl_seconds)
        return max(self.STARTUP_RECONNECT_GRACE_SECONDS, ttl // 4)

    def _startup_reconnect_candidates(self) -> list[dict[str, Any]]:
        # 接管缓冲远小于 stale cleanup 的 grace（15s）：recovery 必须先于
        # stale 动手，否则店铺被标 offline 后就失去恢复资格；多 worker
        # 竞态由 claim SQL 的 UPDATE WHERE 原子性保证，缓冲不是正确性依赖
        return self.repos.shops.startup_reconnect_candidates(
            worker_id=self.worker_id,
            grace_seconds=self.STARTUP_TAKEOVER_BUFFER_SECONDS,
        )

    def _claim_startup_reconnect_lease(self, shop_id: int) -> bool:
        expires_at = datetime.now() + timedelta(seconds=int(self.config.runtime.lease_ttl_seconds))
        updated = self.repos.shops.claim_startup_reconnect_lease(
            int(shop_id),
            worker_id=self.worker_id,
            pid=os.getpid(),
            expires_at=expires_at,
            grace_seconds=self.STARTUP_TAKEOVER_BUFFER_SECONDS,
        )
        if not updated:
            return False
        with self._lock:
            self._owned_shop_ids.add(int(shop_id))
        return True

    def _auto_reconnect_shop_after_restart(self, shop_id: int) -> None:
        shop_id = int(shop_id)
        if not self._claim_startup_reconnect_lease(shop_id):
            if not self._has_active_runtime_lease(shop_id):
                self._mark_startup_reconnect_offline(
                    shop_id,
                    "runtime worker restarted; reconnect lease could not be claimed",
                )
            self.runtime_logger.log(
                "WARNING",
                __name__,
                "runtime.startup_reconnect.skip",
                "startup reconnect skipped because lease could not be claimed",
                shop_id=shop_id,
                context={"worker_id": self.worker_id},
            )
            return

        runner = self.runner(shop_id)
        # 手动上线可能在本线程等待期间抢先接管——已持有活连接时
        # 不得重连（千牛同账号重连会断开刚建立的会话）
        if runner.has_live_connection():
            self.runtime_logger.log(
                "INFO",
                __name__,
                "runtime.startup_reconnect.skip",
                "shop already connected by manual online, skip reconnect",
                shop_id=shop_id,
                context={"worker_id": self.worker_id},
            )
            return
        try:
            runner._set_shop_status("connecting")
            self.hub.publish({"type": "shop_status", "data": runner._shop_row()})
            runner._connect_customer_service_from_cache()
            self.runtime_logger.log(
                "INFO",
                __name__,
                "runtime.startup_reconnect.success",
                "startup reconnect succeeded",
                shop_id=shop_id,
                mall_id=runner.mall_id,
                context={"worker_id": self.worker_id},
            )
        except Exception as exc:
            verification_required = (
                self._is_verification_required_error(exc)
                or self._is_verification_required_payload(runner.token_result)
            )
            message = (
                "登录态需要验证，已取消自动上线，请重新登入"
                if verification_required
                else str(exc)
            )
            session_id = runner.session_id
            try:
                runner._disconnect_customer_service(update_session=True, session_status="offline")
            except Exception:
                pass
            if session_id:
                self.repos.shops.end_session_with_error(session_id, message)
            self._mark_startup_reconnect_offline(shop_id, message)
            self.runtime_logger.log(
                "WARNING" if verification_required else "ERROR",
                __name__,
                "runtime.startup_reconnect.cancelled" if verification_required else "runtime.startup_reconnect.failed",
                message,
                shop_id=shop_id,
                mall_id=runner.mall_id,
                error=exc,
                context={"worker_id": self.worker_id, "verification_required": verification_required},
            )

    def _mark_startup_reconnect_offline(self, shop_id: int, message: str) -> None:
        self.repos.shops.mark_shop_offline_with_error(shop_id, message)
        self.repos.shops.end_shop_sessions_with_error(shop_id, message)
        self.release_shop_lease(shop_id, status="offline")
        self.repos.shops.force_offline_lease(shop_id, worker_id=self.worker_id)
        shop = self.repos.shops.row_with_cache(shop_id)
        if shop:
            self.hub.publish({"type": "shop_status", "data": shop})

    def _has_active_runtime_lease(self, shop_id: int) -> bool:
        return self.repos.shops.has_active_lease(shop_id)

    @staticmethod
    def _is_verification_required_error(exc: Exception) -> bool:
        try:
            text = json.dumps(getattr(exc, "args", (str(exc),)), ensure_ascii=False, default=str)
        except Exception:
            text = str(exc)
        text = f"{text} {exc}".lower()
        markers = (
            "need_verify",
            "need verify",
            "verify_code",
            "verifycode",
            "mobileverification",
            "force mobile verify",
            "forcemobileverify",
            "identityverify",
            "identity verify",
            "identityverifyurl",
            "loginlimitstatus",
            "captcha",
            "risk verify",
            "risk verification",
            "验证码",
            "需要验证码",
            "身份验证",
            "手机验证",
            "图形验证",
            "风控验证",
            "安全验证",
            "需要验证",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _is_verification_required_payload(cls, value: Any) -> bool:
        if value in (None, "", [], {}):
            return False
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return cls._is_verification_required_error(RuntimeError(text))

    def _lease_heartbeat_loop(self) -> None:
        interval = max(5, int(self.config.runtime.lease_ttl_seconds) // 3)
        while not self._stop_event.wait(interval):
            with self._lock:
                shop_ids = list(self._owned_shop_ids)
            if not shop_ids:
                continue
            expires_at = datetime.now() + timedelta(seconds=int(self.config.runtime.lease_ttl_seconds))
            for shop_id in shop_ids:
                try:
                    self.repos.shops.heartbeat_lease(
                        shop_id, worker_id=self.worker_id, pid=os.getpid(), expires_at=expires_at,
                    )
                except Exception as exc:
                    # DB 瞬时故障不能让心跳线程死亡（否则租约过期后其他 worker
                    # 会接管同一店铺，本进程 WS 仍在运行 → 双连接双发）
                    self.runtime_logger.log(
                        "ERROR",
                        __name__,
                        "runtime.lease_heartbeat",
                        "lease heartbeat failed, will retry next round",
                        shop_id=shop_id,
                        error=exc,
                    )

    def _stale_online_cleanup_loop(self) -> None:
        """定期清理'假在线'店铺：DB 显示 online/connecting 但租约不存在或已过期。

        场景：worker 进程崩溃/被 kill 后快速重启（旧租约未过 grace 不接管）、
        心跳线程死亡等，店铺状态停留在 online 但没有任何 WS 连接。
        """
        grace_seconds = self._startup_reconnect_grace_seconds()
        while not self._stop_event.wait(60):
            try:
                stale_shop_ids = self.repos.shops.stale_online_shop_ids(grace_seconds=grace_seconds)
            except Exception as exc:
                self.runtime_logger.log(
                    "ERROR",
                    __name__,
                    "runtime.stale_cleanup",
                    "stale online cleanup query failed",
                    error=exc,
                )
                continue
            for shop_id in stale_shop_ids:
                with self._lock:
                    runner = self._runners.get(shop_id)
                # 本进程 runner 仍持有活连接时跳过（平台无关判定：
                # 心跳抖动不应把真在线的店铺误杀——那会留下 DB 已下线
                # 但 WS 仍在跑的双连接隐患）
                if runner is not None and getattr(runner, "has_live_connection", lambda: False)():
                    continue
                try:
                    self.repos.shops.mark_shop_offline_with_error(
                        shop_id, "检测到店铺实际不在线，已自动下线；如登录态仍有效可直接点击上线",
                    )
                    self.repos.shops.end_shop_sessions_with_error(shop_id, 'stale online cleanup')
                    self.repos.shops.expire_offline_lease(shop_id)
                    shop = self.repos.shops.row_with_cache(shop_id)
                    if shop:
                        self.hub.publish({"type": "shop_status", "data": shop})
                    self.runtime_logger.log(
                        "WARNING",
                        __name__,
                        "runtime.stale_cleanup",
                        "stale online shop marked offline",
                        shop_id=shop_id,
                    )
                except Exception as exc:
                    self.runtime_logger.log(
                        "ERROR",
                        __name__,
                        "runtime.stale_cleanup",
                        "failed to mark stale shop offline",
                        shop_id=shop_id,
                        error=exc,
                    )

    def _daily_reconnect_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            target = now.replace(hour=15, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = max(1.0, (target - now).total_seconds())
            if self._stop_event.wait(wait_seconds):
                return
            with self._lock:
                runners = list(self._runners.values())
            for runner in runners:
                if self._stop_event.is_set():
                    return
                try:
                    runner.hot_reconnect_customer_service(reason="daily_15_00")
                except Exception as exc:
                    self.runtime_logger.log(
                        "ERROR",
                        __name__,
                        "runtime.daily_reconnect",
                        "scheduled websocket reconnect failed",
                        shop_id=runner.shop_id,
                        mall_id=runner.mall_id,
                        error=exc,
                    )
                self._stop_event.wait(2)

    def runtime_workers(self) -> list[dict[str, Any]]:
        return self.repos.shops.runtime_workers()

    def shop_owner(self, shop_id: int) -> dict[str, Any] | None:
        return self.repos.shops.shop_owner(int(shop_id))

    def handle_runtime_command(self, command: dict[str, Any]) -> bool:
        action = str(command.get("action") or "")
        shop_id = command.get("shop_id")
        params = command.get("params") if isinstance(command.get("params"), dict) else {}
        reply_channel = str(command.get("reply_channel") or "")
        command_id = str(command.get("id") or "")
        if not reply_channel or not command_id:
            return False
        if shop_id is not None:
            shop_id = int(shop_id)
            lease_actions = {"start_shop", "login_shop", "password_login_shop", "password_login_verify", "online_shop"}
            if action in lease_actions:
                if not self.acquire_shop_lease(shop_id):
                    return False
            elif not self.owns_shop(shop_id):
                return False
        try:
            result = self._execute_runtime_action(action, shop_id, params)
        except Exception as exc:
            self.command_bus.reply(reply_channel, command_id, ok=False, error=str(exc))
            return True
        self.command_bus.reply(reply_channel, command_id, ok=True, result=result)
        return True

    def _execute_runtime_action(self, action: str, shop_id: int | None, params: dict[str, Any]) -> Any:
        if action == "start_shop":
            return self.start_shop(shop_id)
        if action == "login_shop":
            return self.login_shop(shop_id)
        if action == "password_login_shop":
            return self.password_login_shop(shop_id, str(params.get("username") or ""), str(params.get("password") or ""))
        if action == "password_login_send_sms":
            return self.password_login_send_sms(shop_id)
        if action == "password_login_verify":
            return self.password_login_verify(shop_id, str(params.get("verify_code") or ""))
        if action == "online_shop":
            return self.online_shop(shop_id)
        if action == "offline_shop":
            self.offline_shop(shop_id)
            return {"ok": True}
        if action == "stop_shop":
            self.stop_shop(shop_id)
            return {"ok": True}
        if action == "transfer_services":
            return self.runner(shop_id).get_assignable_services()
        if action == "conversation_context":
            return self.runner(shop_id).conversation_context(int(params["conversation_id"]))
        if action == "send_reply":
            return self.runner(shop_id).send_reply(conversation_id=int(params["conversation_id"]), content=str(params.get("content") or ""))
        if action == "send_image":
            return self.runner(shop_id).send_image(
                conversation_id=int(params["conversation_id"]),
                image_base64=str(params.get("image_base64") or ""),
            )
        if action == "transfer":
            return self.runner(shop_id).transfer(
                conversation_id=int(params["conversation_id"]),
                csid=str(params.get("csid") or ""),
                remark=str(params.get("remark") or ""),
            )
        raise RuntimeError(f"unsupported runtime action: {action}")

    def command_loop(self) -> None:
        self.start_background_services()
        self.command_bus.listen(self.handle_runtime_command, stop_checker=self._stop_event.is_set)

