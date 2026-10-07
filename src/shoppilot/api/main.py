"""FastAPI entrypoint (Step Q): logging, request_id middleware, error handlers, and every router under /v1.

What the lifespan puts on `app.state` (the dependencies in api/deps.py read it from there):

    session_factory  the application database (tickets, approvals, audit ...)
    shop             the store backend. MockShop for now; the Shopify backend plugs in here later
    limits           refund limits from configs/settings.dev.yaml
    checkpointer     the Postgres saver (Step N)
    graph            the compiled SUPERVISOR graph (it runs the support, inventory, listing and reports graphs)

If the database is down the API still starts (docs, /health), `graph` stays None and runs are refused with a clean 503.
`/ready` then answers 503 as well, so a load balancer or Docker health check can tell.
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from shoppilot.agents.checkpoint import close_checkpointer, open_checkpointer
from shoppilot.agents.supervisor import build_supervisor_graph
from shoppilot.api.routers import ALL_ROUTERS
from shoppilot.core.config import settings
from shoppilot.core.errors import OrderNotFound, register_exception_handlers
from shoppilot.core.logging import bind_context, get_logger, setup_logging
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.policy.limits import load_limits
from shoppilot.shop.factory import make_shop

log = get_logger("shoppilot.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_console)
    log.info("api starting", extra={"env": settings.env})

    # The engine does not connect until the first query, so this never fails when the database is down.
    engine = make_engine()
    factory = make_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.shop = make_shop(factory)  # SHOP_STORE_BACKEND=mock (default) or shopify
    app.state.limits = load_limits()

    # Step N: the Postgres checkpointer and the supervisor graph live for the whole life of the process.
    app.state.checkpointer = None
    app.state.graph = None
    try:
        saver = await run_in_threadpool(open_checkpointer)  # blocking connect, so not on the event loop
        app.state.checkpointer = saver
        app.state.graph = build_supervisor_graph(saver)
        log.info("checkpointer ready")
    except Exception:
        log.warning("checkpointer unavailable: agent runs cannot start", exc_info=True)

    yield

    if app.state.checkpointer is not None:
        await run_in_threadpool(close_checkpointer, app.state.checkpointer)
    engine.dispose()
    log.info("api stopped")


app = FastAPI(title="ShopPilot", version="0.1.0", lifespan=lifespan)
register_exception_handlers(app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    with bind_context(request_id=rid):
        response = await call_next(request)
        ms = round((time.perf_counter() - start) * 1000, 1)
        log.info("%s %s -> %s", request.method, request.url.path, response.status_code, extra={"ms": ms})
    response.headers["x-request-id"] = rid
    return response


for _router in ALL_ROUTERS:  # /health and /ready live in routers/health.py
    app.include_router(_router)


if settings.env == "dev":  # sirf logging/error test karne ke liye

    @app.get("/v1/debug/not-found")
    async def debug_not_found() -> None:
        raise OrderNotFound("order 99999 not found", details={"order_ref": "99999"})

    @app.get("/v1/debug/boom")
    async def debug_boom() -> None:
        return 1 / 0  # type: ignore[return-value]


# Local dev: Caddy ke bagair UI chalane ke liye. Production mein Caddy yehi kaam karta hai
# (ui/ serve karna aur /api hata kar API ko bhejna), is liye ye sirf dev mein hai.
UI_DIR = Path(__file__).resolve().parents[3] / "ui"

if settings.env == "dev" and UI_DIR.is_dir():

    @app.middleware("http")
    async def strip_api_prefix(request: Request, call_next):
        path = request.scope["path"]
        if path.startswith("/api/"):
            request.scope["path"] = path[4:]  # /api/v1/tickets -> /v1/tickets
        return await call_next(request)

    # Sab routes ke BAAD mount hona zaroori hai, warna ye unhe dhak lega.
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
