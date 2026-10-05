"""FastAPI entrypoint (minimal): logging + request_id middleware + error handlers + health. Baqi routers baad mein."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from shoppilot.agents.checkpoint import close_checkpointer, open_checkpointer
from shoppilot.agents.support import build_support_graph
from shoppilot.core.config import settings
from shoppilot.core.errors import OrderNotFound, register_exception_handlers
from shoppilot.core.logging import bind_context, get_logger, setup_logging

log = get_logger("shoppilot.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_console)
    log.info("api starting", extra={"env": settings.env})

    # Step N: the Postgres checkpointer and the support graph live for the whole life of the process.
    # If the database is down the API still starts (health, docs), but app.state.graph stays None and runs are refused.
    app.state.checkpointer = None
    app.state.graph = None
    try:
        saver = await run_in_threadpool(open_checkpointer)  # blocking connect, so not on the event loop
        app.state.checkpointer = saver
        app.state.graph = build_support_graph(saver)
        log.info("checkpointer ready")
    except Exception:
        log.warning("checkpointer unavailable: agent runs cannot start", exc_info=True)

    yield

    if app.state.checkpointer is not None:
        await run_in_threadpool(close_checkpointer, app.state.checkpointer)
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    return {"status": "ready"}


if settings.env == "dev":  # sirf logging/error test karne ke liye

    @app.get("/v1/debug/not-found")
    async def debug_not_found() -> None:
        raise OrderNotFound("order 99999 not found", details={"order_ref": "99999"})

    @app.get("/v1/debug/boom")
    async def debug_boom() -> None:
        return 1 / 0  # type: ignore[return-value]
