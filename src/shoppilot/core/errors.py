"""Application exceptions + FastAPI handlers (write once).

Rule: code raises subclasses of AppError. The API turns them into a clean JSON error
and writes the traceback to logs/error.log. The stack trace is never sent to the client.
Agent tools can turn these errors into {"ok": False, "error": code} (see .to_result()).
"""
from __future__ import annotations

from typing import Any

from shoppilot.core.logging import get_logger, request_id_var

log = get_logger(__name__)


class AppError(Exception):
    """Base class. code = machine readable, message = human readable, http_status = API response."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None, code: str | None = None):
        self.message = message or self.__class__.__name__
        self.details = details or {}
        if code:
            self.code = code
        super().__init__(self.message)

    def to_result(self) -> dict[str, Any]:
        """Tool-friendly shape the LLM can read: {"ok": False, "error": "ORDER_NOT_FOUND"}"""
        return {"ok": False, "error": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


# --- config / infra -----------------------------------------------------------
class ConfigError(AppError):
    code, http_status = "CONFIG_ERROR", 500

class DatabaseError(AppError):
    code, http_status = "DATABASE_ERROR", 503

# --- auth ---------------------------------------------------------------------
class AuthError(AppError):
    code, http_status = "UNAUTHENTICATED", 401

class PermissionDenied(AppError):
    code, http_status = "FORBIDDEN", 403

class RateLimited(AppError):
    code, http_status = "RATE_LIMITED", 429

# --- request / business -------------------------------------------------------
class ValidationFailed(AppError):
    code, http_status = "VALIDATION_ERROR", 422

class NotFound(AppError):
    code, http_status = "NOT_FOUND", 404

class OrderNotFound(NotFound):
    code = "ORDER_NOT_FOUND"

class TicketNotFound(NotFound):
    code = "TICKET_NOT_FOUND"

class PolicyDenied(AppError):
    code, http_status = "POLICY_DENIED", 409

class ApprovalRequired(AppError):
    code, http_status = "APPROVAL_REQUIRED", 409

class IdempotencyConflict(AppError):
    code, http_status = "IDEMPOTENCY_CONFLICT", 409

class BudgetExceeded(AppError):
    code, http_status = "BUDGET_EXCEEDED", 429

# --- tools / external ---------------------------------------------------------
class ToolError(AppError):
    code, http_status = "TOOL_ERROR", 502

class ToolTimeout(ToolError):
    code, http_status = "TOOL_TIMEOUT", 504

class ShopBackendError(ToolError):
    code = "SHOP_BACKEND_ERROR"

class LLMError(AppError):
    code, http_status = "LLM_ERROR", 502

class LLMRateLimited(LLMError):
    code, http_status = "LLM_RATE_LIMITED", 503

class GuardrailViolation(AppError):
    code, http_status = "GUARDRAIL_VIOLATION", 400


# --- FastAPI wiring -----------------------------------------------------------
def _body(code: str, message: str, request_id: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        err["details"] = details
    return {"error": err}


def register_exception_handlers(app: Any) -> None:
    """app = FastAPI(). Call once in api/main.py."""
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def rid(request: Request) -> str:
        return getattr(request.state, "request_id", None) or request_id_var.get()

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        extra = {"code": exc.code, "path": request.url.path, "details": exc.details}
        if exc.http_status >= 500:
            log.error("%s: %s", exc.code, exc.message, exc_info=exc, extra=extra)
            message = "Something went wrong on our side." if exc.http_status == 500 else exc.message
        else:
            log.warning("%s: %s", exc.code, exc.message, extra=extra)   # client error: warning, no traceback
            message = exc.message
        return JSONResponse(_body(exc.code, message, rid(request), exc.details if exc.http_status < 500 else None), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        log.warning("request validation failed", extra={"path": request.url.path, "errors": errors})
        return JSONResponse(_body("VALIDATION_ERROR", "Invalid request.", rid(request), {"errors": errors}), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        log.warning("http %s on %s", exc.status_code, request.url.path)
        return JSONResponse(_body(f"HTTP_{exc.status_code}", str(exc.detail), rid(request)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled exception on %s %s", request.method, request.url.path, exc_info=exc, extra={"path": request.url.path})
        return JSONResponse(_body("INTERNAL_ERROR", "Something went wrong on our side.", rid(request)), status_code=500)
