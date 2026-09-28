"""FastAPI application factory: assembles middleware, routers, WS and static files."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from openkefu.platforms.pdd.chat.knowledge import KnowledgeService, NoteSetService
from openkefu.platforms.pdd.config import PROJECT_ROOT
from openkefu.web.config import load_config
from openkefu.web.context import AppContext
from openkefu.web.db import Database
from openkefu.web.logging_config import configure_logging
from openkefu.web.realtime import RealtimeHub, RuntimeLogger
from openkefu.web.routers import auth, chat, knowledge, logs, return_records, shops, status, users
from openkefu.web.runtime import ShopRuntimeManager
from openkefu.web.security import decode_token

WS_AUTH_PROTOCOL_PREFIX = "openkefu-auth."


def create_app() -> FastAPI:
    config = load_config()
    configure_logging(config)
    db = Database(config)
    db.initialize()
    db.execute(
        """
        DELETE rr_old FROM return_records rr_old
        JOIN return_records rr_new
          ON rr_old.shop_id=rr_new.shop_id
         AND rr_old.user_uid=rr_new.user_uid
         AND rr_old.order_no=rr_new.order_no
         AND rr_old.id < rr_new.id
        WHERE rr_old.user_uid <> '' AND rr_old.order_no <> ''
        """
    )
    hub = RealtimeHub(config)
    runtime_logger = RuntimeLogger(db, hub)
    runtime = ShopRuntimeManager(db, hub, runtime_logger, config)
    knowledge = KnowledgeService(db, config)
    note_service = NoteSetService(db)

    ctx = AppContext(
        config=config,
        db=db,
        hub=hub,
        runtime_logger=runtime_logger,
        runtime=runtime,
        knowledge=knowledge,
        note_service=note_service,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await hub.start()
        if config.runtime.role in {"worker", "both"}:
            runtime.start_background_services()
        try:
            yield
        finally:
            runtime.stop_background_services()
            ctx.offline_pool.shutdown(wait=False)
            await hub.stop()

    app = FastAPI(title="OpenKefu Web", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.db = db
    app.state.hub = hub
    app.state.runtime_logger = runtime_logger
    app.state.runtime = runtime
    app.state.knowledge = knowledge
    app.state.note_service = note_service

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
            "font-src 'self' data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' https://cdn.jsdelivr.net; connect-src 'self' ws: wss:",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    allowed_origins = config.security.allowed_origins
    if allowed_origins:
        # 显式配置 allowed_origins 时才启用 CORS；不配置则视为同源部署。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(allowed_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for builder in (auth.build_router, users.build_router, shops.build_router, chat.build_router,
                    return_records.build_router, knowledge.build_router, logs.build_router, status.build_router):
        app.include_router(builder(ctx))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        selected_subprotocol = ""
        try:
            token = ""
            for subprotocol in websocket.scope.get("subprotocols") or []:
                if subprotocol.startswith(WS_AUTH_PROTOCOL_PREFIX):
                    selected_subprotocol = subprotocol
                    token = subprotocol[len(WS_AUTH_PROTOCOL_PREFIX):]
                    break
            if not token:
                await websocket.close(code=4401)
                return
            payload = decode_token(token, secret=config.security.jwt_secret)
            user = await asyncio.to_thread(ctx.active_user_from_payload, payload)
            shop_ids = set() if user["role"] == "admin" else await asyncio.to_thread(ctx.assigned_shop_ids, user)
        except (HTTPException, ValueError):
            await websocket.close(code=4401)
            return
        await hub.connect(
            websocket,
            user_id=int(user["id"]),
            role=str(user["role"]),
            shop_ids=shop_ids,
            subprotocol=selected_subprotocol,
        )
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await hub.disconnect(websocket)

    dist = PROJECT_ROOT / config.server.frontend_dist
    if dist.exists() and not os.environ.get("OPENKEFU_DEV"):
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app
