"""Logging setup. Call setup_logging() once at app start.

Files:
  logs/app.log    every log (INFO+), one JSON object per line
  logs/error.log  only ERROR/CRITICAL, with the full traceback
  console         short readable lines (development)

Every line has request_id, ticket_id and thread_id. Emails and secrets are masked.

    log = get_logger(__name__)
    with bind_context(ticket_id="T-1042"):
        log.info("refund proposed", extra={"amount_pkr": 5400})
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---- context ids ----
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
ticket_id_var: ContextVar[str] = ContextVar("ticket_id", default="-")
thread_id_var: ContextVar[str] = ContextVar("thread_id", default="-")
_CTX_VARS = {"request_id": request_id_var, "ticket_id": ticket_id_var, "thread_id": thread_id_var}


@contextmanager
def bind_context(**values: str) -> Iterator[None]:
    """Set request_id / ticket_id / thread_id for all logs inside the block."""
    tokens = [(_CTX_VARS[k], _CTX_VARS[k].set(str(v))) for k, v in values.items() if k in _CTX_VARS]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# ---- masking ----
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_RE = re.compile(r"(?i)((?:api[_-]?key|secret|token|password|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)")


def mask(text: str) -> str:
    """a***@gmail.com, api_key=***, Bearer ***"""
    text = _EMAIL_RE.sub(r"\1***@\2", text)
    text = _BEARER_RE.sub(r"\1***", text)
    return _SECRET_RE.sub(r"\1***", text)


# ---- handlers' helpers ----
class ContextFilter(logging.Filter):
    """Adds the context ids to the record and masks the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CTX_VARS.items():
            setattr(record, name, var.get())
        if not getattr(record, "_masked", False):
            record.msg = mask(record.getMessage())
            record.args = ()
            record._masked = True  # type: ignore[attr-defined]
        return True


_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | set(_CTX_VARS) | {"message", "asctime", "_masked", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "ticket_id": getattr(record, "ticket_id", "-"),
            "thread_id": getattr(record, "thread_id", "-"),
            "where": f"{record.module}.{record.funcName}:{record.lineno}",
        }
        if extra := {k: v for k, v in record.__dict__.items() if k not in _STANDARD}:
            data["extra"] = json.loads(mask(json.dumps(extra, default=str)))
        if record.exc_info and record.exc_info[0]:
            data["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": mask(str(record.exc_info[1])),
                "traceback": mask(self.formatException(record.exc_info)),
            }
        return json.dumps(data, ensure_ascii=False, default=str)


class MaskedFormatter(logging.Formatter):
    """Plain text formatter that also masks the traceback."""

    def formatException(self, ei: Any) -> str:  # noqa: N802
        return mask(super().formatException(ei))


# ---- setup ----
def setup_logging(level: str = "INFO", log_dir: str | Path = "logs", console: bool = True) -> Path:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    app_file = logging.handlers.RotatingFileHandler(log_path / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    app_file.setFormatter(JsonFormatter())

    error_file = logging.handlers.RotatingFileHandler(log_path / "error.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(MaskedFormatter("%(asctime)s %(levelname)s %(name)s [req=%(request_id)s ticket=%(ticket_id)s]: %(message)s"))

    handlers: list[logging.Handler] = [app_file, error_file]
    if console:
        screen = logging.StreamHandler(sys.stderr)
        screen.setFormatter(MaskedFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"))
        handlers.append(screen)

    for h in handlers:
        h.addFilter(ContextFilter())
        root.addHandler(h)

    # uvicorn logs go through the root logger, so they reach the files too
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or "shoppilot")
