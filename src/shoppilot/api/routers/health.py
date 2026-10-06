"""Liveness and readiness probes (public, not under /v1).

/health: the process is up. Never touches the database, so a database outage does not make Docker restart the API.
/ready: the API can really work: the database answers and the agent graph (checkpointer) started. 503 otherwise.
The body only says ok or down: no error text, no connection string.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from shoppilot.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    checks = {"database": "down", "graph": "down"}

    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        try:
            with factory() as session:
                session.execute(text("select 1"))
            checks["database"] = "ok"
        except Exception:
            log.warning("readiness: the database does not answer", exc_info=True)

    if getattr(request.app.state, "graph", None) is not None:
        checks["graph"] = "ok"

    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse({"status": "ready" if is_ready else "not_ready", **checks}, status_code=200 if is_ready else 503)
