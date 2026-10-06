"""Checkpointing and crash recovery (Step N).

The Postgres checkpointer saves the graph state after every node. A run that waits for a human (interrupt) survives a
server restart, and can be resumed days later with Command(resume=...) on the same thread_id (= ticket id).

Why the SYNC saver (PostgresSaver) and not the async one: every node and every tool in this project is sync code
(sync SQLAlchemy, sync tools), and psycopg async mode does not work on the default Windows event loop. The API runs
the graph in a worker thread (Step Q). The saver uses a connection pool, so several runs can use it at once.

What is here:
- open_checkpointer / close_checkpointer: build the pool and the saver, create the checkpoint tables once (setup)
- get_ticket_state: current node, pending interrupt and a few safe fields, for the UI
- get_ticket_history: the checkpoints of one ticket, oldest first, for the UI timeline
- prune_checkpoints: delete the threads whose newest checkpoint is older than N days (retention, run by a scheduled job)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from shoppilot.core.config import settings
from shoppilot.core.logging import get_logger

log = get_logger(__name__)

OPEN_TIMEOUT_S = 5.0  # a database that is down must fail fast at start-up, not hang
RETENTION_DAYS = 30
# The checkpointer needs autocommit connections and dict rows. prepare_threshold=0 avoids prepared-statement clashes.
CONNECTION_KWARGS: dict[str, Any] = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


def open_checkpointer(uri: str | None = None, *, setup: bool = True, timeout_s: float = OPEN_TIMEOUT_S) -> PostgresSaver:
    """Open a connection pool and return a PostgresSaver. setup=True creates the checkpoint tables if they are missing.

    Raises when the database cannot be reached within timeout_s. The caller owns the saver: close it with
    close_checkpointer when the process stops.
    """
    pool = ConnectionPool(
        uri or settings.psycopg_uri,
        min_size=1,
        max_size=10,
        kwargs=CONNECTION_KWARGS,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=timeout_s)
        saver = PostgresSaver(pool)  # type: ignore[arg-type]
        if setup:
            saver.setup()
    except Exception:
        pool.close()
        raise
    return saver


def _pool(saver: PostgresSaver) -> ConnectionPool[Any]:  # Any: the rows are dicts (dict_row), not tuples
    pool = saver.conn
    if not isinstance(pool, ConnectionPool):  # open_checkpointer always builds a pool
        raise TypeError("this saver was not made by open_checkpointer")
    return pool


def close_checkpointer(saver: PostgresSaver) -> None:
    _pool(saver).close()


# -------------------------------------- read the state
class TicketSnapshot(BaseModel):
    """What the UI may know about a ticket's run. Small and safe: no messages, no customer text."""

    ticket_id: str
    exists: bool  # False when the graph never ran for this ticket (or its checkpoints were pruned)
    next_nodes: list[str] = Field(default_factory=list)  # the node(s) that run next; ["approval_gate"] while it waits
    finished: bool = False  # the graph reached its end
    pending_interrupt: dict[str, Any] | None = None  # the approval request the graph is waiting on
    intent: str | None = None
    tier: str | None = None  # what the policy engine ruled: auto | manager | owner | deny | none
    approval_status: str | None = None
    escalated: bool = False
    verified: bool = False
    errors: list[str] = Field(default_factory=list)


class HistoryItem(BaseModel):
    """One saved checkpoint of a ticket."""

    step: int
    source: str  # input | loop | update | fork
    next_nodes: list[str] = Field(default_factory=list)
    created_at: str | None = None


def _config(ticket_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": ticket_id}}  # thread_id is the ticket id


def get_ticket_state(graph: Any, ticket_id: str) -> TicketSnapshot:
    """Where is this ticket's run? Reads the latest checkpoint; it does not run anything."""
    snap = graph.get_state(_config(ticket_id))
    if snap.created_at is None:  # LangGraph returns an empty snapshot for an unknown thread
        return TicketSnapshot(ticket_id=ticket_id, exists=False)

    values: dict[str, Any] = snap.values or {}
    pending: dict[str, Any] | None = None
    for task in snap.tasks:
        if task.interrupts:
            value = task.interrupts[0].value
            pending = value if isinstance(value, dict) else {"value": value}
            break

    next_nodes = list(snap.next)
    return TicketSnapshot(
        ticket_id=ticket_id,
        exists=True,
        next_nodes=next_nodes,
        finished=not next_nodes,
        pending_interrupt=pending,
        intent=values.get("intent"),
        tier=(values.get("ruling") or {}).get("tier"),
        approval_status=(values.get("approval") or {}).get("status"),
        escalated=bool(values.get("escalated")),
        verified=bool(values.get("verified")),
        errors=[str(e) for e in values.get("errors", [])],
    )


def get_ticket_history(graph: Any, ticket_id: str, limit: int = 50) -> list[HistoryItem]:
    """The checkpoints of one ticket, oldest first. Each one says which node was about to run."""
    items: list[HistoryItem] = []
    for snap in graph.get_state_history(_config(ticket_id), limit=limit):
        meta = snap.metadata or {}
        items.append(
            HistoryItem(
                step=int(meta.get("step", -1)),
                source=str(meta.get("source", "")),
                next_nodes=list(snap.next),
                created_at=snap.created_at,
            )
        )
    items.reverse()  # LangGraph returns the newest first
    return items


# --------------------------------------------------------------------------------------------------- retention
OLD_THREADS_SQL = """
SELECT thread_id
FROM checkpoints
GROUP BY thread_id
HAVING max((checkpoint ->> 'ts')::timestamptz) < %s
"""


def prune_checkpoints(saver: PostgresSaver, days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
    """Delete every thread whose newest checkpoint is older than `days`. Returns how many threads were deleted.

    A thread that still waits for an approval is old after 30 days only if nobody ever decided: approvals expire after
    48 hours, so such a thread is stale anyway. The audit_log and the approvals table are NOT touched.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    with _pool(saver).connection() as conn:
        rows = conn.execute(OLD_THREADS_SQL, (cutoff,)).fetchall()
    thread_ids = [str(row["thread_id"]) for row in rows]
    for thread_id in thread_ids:
        saver.delete_thread(thread_id)
    log.info("checkpoints pruned", extra={"threads": len(thread_ids), "older_than_days": days})
    return len(thread_ids)
