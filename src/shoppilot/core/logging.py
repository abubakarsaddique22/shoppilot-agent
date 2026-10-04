"""Central logging setup (write once, use everywhere).

Teen jagah log likhta hai:
  * console          -> development mein terminal par readable lines
  * logs/app.log     -> HAR log (INFO+), JSON lines, machine-readable
  * logs/error.log   -> sirf ERROR / CRITICAL + poora traceback, insaan ke parhne layak

Har line mein request_id / ticket_id / thread_id aati hai (contextvars se).
Emails aur secrets (api key, token, password, Bearer ...) automatically mask hote hain.

Use:
    from shoppilot.core.logging import setup_logging, get_logger, bind_context
    setup_logging()                       # app start par EK baar
    log = get_logger(__name__)
    with bind_context(ticket_id="T-1042"):
        log.info("refund proposed", extra={"amount_pkr": 5400})
    try: ...
    except Exception: log.exception("refund failed")   # traceback error.log mein jayega
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------- context ---
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
ticket_id_var: ContextVar[str] = ContextVar("ticket_id", default="-")
thread_id_var: ContextVar[str] = ContextVar("thread_id", default="-")
_CTX_VARS = {"request_id": request_id_var, "ticket_id": ticket_id_var, "thread_id": thread_id_var}


@contextmanager
def bind_context(**values: str) -> Iterator[None]:
    """Temporarily set request_id / ticket_id / thread_id for all logs inside the block."""
    tokens = [(_CTX_VARS[k], _CTX_VARS[k].set(str(v))) for k, v in values.items() if k in _CTX_VARS]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# ---------------------------------------------------------------- masking ---
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|password|passwd|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"
)


def mask(text: str) -> str:
    """a***@gmail.com ; api_key=*** ; Bearer ***"""
    text = _EMAIL_RE.sub(r"\1***@\2", text)
    text = _BEARER_RE.sub(r"\1***", text)
    return _SECRET_RE.sub(r"\1***", text)


def _mask_value(v: Any) -> Any:
    if isinstance(v, str):
        return mask(v)
    if isinstance(v, dict):
        return {k: ("***" if re.search(r"(?i)key|secret|token|password", str(k)) else _mask_value(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_mask_value(x) for x in v]
    return v


# ---------------------------------------------------------------- filters ---
class ContextFilter(logging.Filter):
    """Adds request_id/ticket_id/thread_id and masks the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CTX_VARS.items():
            setattr(record, name, var.get())
        if not getattr(record, "_masked", False):
            record.msg = mask(record.getMessage())
            record.args = ()
            record._masked = True  # type: ignore[attr-defined]
        return True


_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "request_id", "ticket_id", "thread_id", "_masked", "taskName",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: _mask_value(v) for k, v in record.__dict__.items() if k not in _STD_ATTRS}


def _format_exc(record: logging.LogRecord) -> str:
    return mask(logging.Formatter().formatException(record.exc_info)) if record.exc_info else ""


# ------------------------------------------------------------- formatters ---
class JsonFormatter(logging.Formatter):
    """One JSON object per line -> logs/app.log."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "ticket_id": getattr(record, "ticket_id", "-"),
            "thread_id": getattr(record, "thread_id", "-"),
            "where": f"{record.module}.{record.funcName}:{record.lineno}",
        }
        if extra := _extras(record):
            data["extra"] = extra
        if record.exc_info and record.exc_info[0]:
            data["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": mask(str(record.exc_info[1])),
                "traceback": _format_exc(record),
            }
        return json.dumps(data, ensure_ascii=False, default=str)


class ErrorBlockFormatter(logging.Formatter):
    """Readable multi-line block -> logs/error.log. Yahan dekho: error kya hai, kahan hua, traceback."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "=" * 78,
            f"TIME      : {ts}",
            f"LEVEL     : {record.levelname}",
            f"LOGGER    : {record.name}  ({record.module}.{record.funcName}:{record.lineno})",
            f"REQUEST   : {getattr(record, 'request_id', '-')}   TICKET: {getattr(record, 'ticket_id', '-')}"
            f"   THREAD: {getattr(record, 'thread_id', '-')}",
            f"MESSAGE   : {record.getMessage()}",
        ]
        if extra := _extras(record):
            lines.append(f"DETAILS   : {json.dumps(extra, ensure_ascii=False, default=str)}")
        if record.exc_info and record.exc_info[0]:
            lines.append(f"EXCEPTION : {record.exc_info[0].__name__}: {mask(str(record.exc_info[1]))}")
            lines.append("TRACEBACK :")
            lines.append(_format_exc(record))
        lines.append("")
        return "\n".join(lines)


class ConsoleFormatter(logging.Formatter):
    _COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[41m"}

    def __init__(self, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        lvl = f"{record.levelname:<8}"
        if self.color:
            lvl = f"{self._COLORS.get(record.levelname, '')}{lvl}\033[0m"
        ctx = " ".join(
            f"{k}={getattr(record, k)}" for k in ("request_id", "ticket_id") if getattr(record, k, "-") != "-"
        )
        line = f"{ts} {lvl} {record.name}: {record.getMessage()}" + (f"  [{ctx}]" if ctx else "")
        if extra := _extras(record):
            line += f"  {extra}"
        if record.exc_info and record.exc_info[0]:
            line += "\n" + _format_exc(record)
        return line


# ------------------------------------------------------------------ setup ---
_CONFIGURED = False


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs", console: bool = True) -> Path:
    """Call ONCE at app start. Safe to call again (it resets handlers). Returns the log directory."""
    global _CONFIGURED
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    ctx_filter = ContextFilter()

    app_h = logging.handlers.RotatingFileHandler(log_path / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    app_h.setLevel(level.upper())
    app_h.setFormatter(JsonFormatter())

    err_h = logging.handlers.RotatingFileHandler(log_path / "error.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    err_h.setLevel(logging.ERROR)
    err_h.setFormatter(ErrorBlockFormatter())

    handlers: list[logging.Handler] = [app_h, err_h]
    if console:
        con_h = logging.StreamHandler(sys.stderr)
        con_h.setFormatter(ConsoleFormatter(color=sys.stderr.isatty()))
        handlers.append(con_h)

    for h in handlers:
        h.addFilter(ctx_filter)
        root.addHandler(h)

    # uvicorn ki apni handlers hata do, root ke through jaye (taake file mein bhi aaye)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_excepthooks()
    _CONFIGURED = True
    logging.getLogger(__name__).info("logging ready", extra={"log_dir": str(log_path.resolve()), "level": level.upper()})
    return log_path


def _install_excepthooks() -> None:
    """Jo exception kahin catch nahi hui wo bhi error.log mein jaye (crash se pehle)."""
    log = logging.getLogger("shoppilot.uncaught")

    def _sys_hook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or "shoppilot")
