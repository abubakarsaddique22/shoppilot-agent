"""Server-Sent Events for agent runs (Step Q).

The graph code is synchronous (sync SQLAlchemy, sync tools, sync Postgres saver, see agents/checkpoint.py), so a run
happens in a WORKER THREAD, and its events travel to the browser through a queue:

    worker thread: work(emit) ---> asyncio.Queue ---> EventStream.events() ---> StreamingResponse ---> browser

Why a dedicated thread and not a sync generator inside StreamingResponse: the run context (`ctx_var`) must be set once
and stay set for the whole run. A sync generator is advanced by a different pool thread on every step, and a
context variable set in one step is gone in the next.

What is NOT in an event: the customer message, the order note, the messages list, secrets. A node update is reduced to a
small whitelist by `summarize_update` (ids, tiers, amounts, codes). The browser still inserts everything with
textContent, because the model-written `summary` of a proposal can repeat a hostile text.

The browser reads the stream with fetch + ReadableStream (EventSource cannot send an Authorization header).
When the browser goes away, the worker keeps running to the end: a graph that stops halfway would leave a ticket in
"working", and the checkpointer makes the finished run safe to read later.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from shoppilot.core.errors import AppError
from shoppilot.core.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_S = 15.0  # a comment line keeps proxies from closing a quiet stream (Caddy: flush_interval -1)
MAX_TEXT = 300
MAX_ITEMS = 8

Emit = Callable[[str, dict[str, Any]], None]
Work = Callable[[Emit], dict[str, Any]]  # runs in the worker thread; its return value becomes the final "end" event

SCALAR_KEYS = (
    "intent",
    "order_ref",
    "route",
    "route_by",
    "route_reason",
    "outcome",
    "verified",
    "escalated",
    "errors",
    "actions_taken",
)
PICKED_KEYS: dict[str, tuple[str, ...]] = {
    "order": ("id", "status"),
    "proposal": ("action", "amount_pkr", "reason", "evidence_ids", "summary"),
    "ruling": ("tier", "allowed_amount", "reasons", "policy_refs", "overridden"),
    "approval": ("status", "by", "note", "amount_pkr", "approval_id"),
    "result": ("refund_id", "order_id", "amount_pkr", "status"),
    "outgoing": ("template", "sent", "error"),
    "draft": ("draft_id", "sku", "qty", "supplier"),
}
INTERRUPT_KEYS = (
    "type",
    "ticket_id",
    "order_id",
    "tier",
    "amount_pkr",
    "reasons",
    "policy_refs",
    "summary",
    "evidence_ids",
    "approval_id",
)


# ------------------------------------------------------------------ formatting
def format_event(event: str, data: dict[str, Any]) -> str:
    """One SSE message. json.dumps escapes every newline, so the data stays on one line."""
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())[:MAX_TEXT]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {str(k): _short(v) for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, list | tuple):
        return [_short(v) for v in list(value)[:MAX_ITEMS]]
    return _short(str(value))


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: _short(source[key]) for key in keys if source.get(key) is not None}


def summarize_update(update: Any) -> dict[str, Any]:
    """The part of a node result the browser may see. Everything not listed here is dropped."""
    if not isinstance(update, dict):
        return {}
    detail: dict[str, Any] = {}
    for key in SCALAR_KEYS:
        value = update.get(key)
        if value is not None and value != []:
            detail[key] = _short(value)
    for key, fields in PICKED_KEYS.items():
        picked = _pick(update.get(key), fields)
        if picked:
            detail[key] = picked
    facts = update.get("facts")
    if isinstance(facts, list) and facts:  # ids only, never the fact data (order note, addresses)
        detail["facts"] = [_short(f.get("id")) for f in facts if isinstance(f, dict)][:MAX_ITEMS]
    return detail


def one_line(detail: dict[str, Any]) -> str:
    """A short text for the timeline row: key=value pairs."""
    parts: list[str] = []
    for key, value in detail.items():
        if isinstance(value, dict):
            parts += [f"{k}={v}" for k, v in value.items() if isinstance(v, str | int | float | bool)]
        elif isinstance(value, list):
            parts.append(f"{key}={', '.join(str(v) for v in value)}")
        else:
            parts.append(f"{key}={value}")
    return " | ".join(parts)[:MAX_TEXT]


def interrupt_payload(raw: Any) -> dict[str, Any]:
    """The approval request the graph is waiting on. `raw` is the interrupt value, or the tuple LangGraph streams."""
    first = raw[0] if isinstance(raw, tuple | list) and raw else raw
    value = getattr(first, "value", first)
    return _pick(value, INTERRUPT_KEYS)


# ------------------------------------------------------------------ the stream
class EventStream:
    """Starts `work` in a worker thread at once and hands out its events as SSE text.

    Create it inside a running event loop (an async endpoint). The thread starts in __init__, so even if the client
    never reads a single byte, the run is not left half done and `work` can release whatever it holds in a finally block.
    """

    def __init__(self, work: Work, heartbeat_s: float = HEARTBEAT_S) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._heartbeat_s = heartbeat_s
        context = contextvars.copy_context()  # the worker keeps the request_id for its logs
        self._future = self._loop.run_in_executor(None, context.run, self._run, work)

    # --- worker thread side
    def _put(self, item: str | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # the loop is already closed (the server is stopping): nobody is listening
            pass

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        self._put(format_event(event, data))

    def _run(self, work: Work) -> None:
        try:
            self._emit("end", work(self._emit))
        except AppError as err:
            log.warning("run stopped: %s", err.code, extra={"code": err.code})
            message = "Something went wrong." if err.http_status == 500 else err.message
            self._emit("error", {"code": err.code, "message": message})
        except Exception:
            log.exception("run failed")
            self._emit("error", {"code": "RUN_FAILED", "message": "The run failed. You can run the ticket again."})
        finally:
            self._put(None)

    # --- event loop side
    async def events(self) -> AsyncIterator[str]:
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._heartbeat_s)
            except TimeoutError:
                yield ": ping\n\n"  # an SSE comment: ignored by the browser, keeps the connection alive
                continue
            if item is None:
                return
            yield item
