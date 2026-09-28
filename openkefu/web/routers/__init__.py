"""API routers, one module per domain. Each exposes ``build_router(ctx)``."""

from openkefu.web.routers import auth, chat, knowledge, logs, return_records, shops, status, users

__all__ = ["auth", "chat", "knowledge", "logs", "return_records", "shops", "status", "users"]
