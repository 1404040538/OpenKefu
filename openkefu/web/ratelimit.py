from __future__ import annotations

import threading
import time


class FixedWindowRateLimiter:
    """进程内固定窗口限流器。

    用于登录/注册/短信验证码等敏感接口的轻量限流（单机部署场景）。
    多 worker 部署时各进程独立计数，仍能显著抬高攻击成本；
    如需严格分布式限流，应在 Redis 上实现。
    """

    def __init__(self, max_hits: int, window_seconds: float, *, enabled: bool = True):
        self.max_hits = max_hits
        self.window_seconds = max(1.0, float(window_seconds))
        self.enabled = enabled
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def hit(self, key: str) -> bool:
        """记录一次访问。返回 True 表示放行，False 表示已超限。"""
        if not self.enabled:
            return True
        now = time.monotonic()
        key = str(key or "")
        with self._lock:
            self._maybe_cleanup(now)
            window_start, count = self._buckets.get(key, (0.0, 0))
            if now - window_start >= self.window_seconds:
                window_start = now
                count = 0
            if count >= self.max_hits:
                return False
            self._buckets[key] = (window_start, count + 1)
            return True

    def remaining(self, key: str) -> int:
        if not self.enabled:
            return self.max_hits
        now = time.monotonic()
        with self._lock:
            window_start, count = self._buckets.get(str(key or ""), (0.0, 0))
            if now - window_start >= self.window_seconds:
                return self.max_hits
            return max(0, self.max_hits - count)

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        expired = [k for k, (window_start, _) in self._buckets.items() if now - window_start >= self.window_seconds]
        for key in expired:
            del self._buckets[key]
