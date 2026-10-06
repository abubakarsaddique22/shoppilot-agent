"""Run the agent on a ticket and stream its progress live (Step Q).

    POST /v1/tickets/{id}/run     support and above; the answer is a Server-Sent Events stream

Events (each one is `event: <name>` plus a JSON `data:` line):

    start      {"ticket_id", "mode"}                 the run begins
    node       {"node", "parent", "status", "ms", "summary", "detail"}   one graph node finished (also the nodes inside
                                                       the support graph: "parent" is then "run_support")
    interrupt  {"type", "tier", "amount_pkr", "approval_id", ...}   the graph stopped and waits for a human
    end        {"status": "finished" | "waiting_approval", "ticket_status", "ms"}
    error      {"code", "message"}                    the run failed; run the ticket again to continue it

"Start or continue" means: the run looks at the saved graph state of the ticket (thread_id = ticket id) and picks a mode.

    start      nothing saved yet            run from the first customer message
    continue   saved but not finished       carry on from the last checkpoint (for example after a crash)
    resume     waiting, and the human has already decided   resume the graph (a decision whose resume had failed)
    waiting    waiting, nobody has decided  nothing to run: report the pending approval and stop
    finished   the graph reached its end    nothing to run: report that and stop

Only one run per ticket at a time (in this process), so a double click cannot start two. The run context (who is asking,
which ticket, which customer) is built from the token and the ticket row, never from the request or from the model.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from shoppilot.agents.checkpoint import get_ticket_state
from shoppilot.api.deps import (
    ClockDep,
    GraphDep,
    LimitsDep,
    SessionFactoryDep,
    ShopDep,
    SupportUser,
    build_run_context,
)
from shoppilot.api.services import first_inbound_text, mark_working, settle_ticket, source_of, ticket_status
from shoppilot.api.sse import Emit, EventStream, Work, interrupt_payload, one_line, summarize_update
from shoppilot.approvals.service import get_approval
from shoppilot.core.config import settings
from shoppilot.core.errors import DatabaseError, IdempotencyConflict, TicketNotFound, ValidationFailed
from shoppilot.core.logging import bind_context, get_logger
from shoppilot.db.models import TicketRow
from shoppilot.tools.context import RunContext, audit, ctx_var

log = get_logger(__name__)
router = APIRouter(prefix="/v1/tickets", tags=["runs"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # no-buffering hint for proxies


class ActiveRuns:
    """Which tickets have a run going in THIS process. One uvicorn process is enough for the portfolio deployment;
    with several workers this would move to a Postgres advisory lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def acquire(self, ticket_id: str) -> bool:
        with self._lock:
            if ticket_id in self._ids:
                return False
            self._ids.add(ticket_id)
            return True

    def release(self, ticket_id: str) -> None:
        with self._lock:
            self._ids.discard(ticket_id)


ACTIVE = ActiveRuns()


class RunPlan(BaseModel):
    """What the run will do, decided from the saved state before anything streams (so errors are normal HTTP errors)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mode: Literal["start", "continue", "resume", "waiting", "finished"]
    customer_email: str
    graph_input: Any = None  # the first message dict, a resume Command, or None (continue from the checkpoint)
    pending: dict[str, Any] | None = None  # the approval request when mode is waiting or resume


def prepare_run(factory: sessionmaker[Session], graph: Any, ticket_id: str) -> RunPlan:
    """Blocking reads (database, checkpointer). Runs in a worker thread, called from the async endpoint."""
    with factory() as session:
        ticket = session.get(TicketRow, ticket_id)
        if ticket is None:
            raise TicketNotFound(f"ticket {ticket_id} not found", details={"ticket_id": ticket_id})
        customer_email, channel = ticket.customer_email, ticket.channel
        text = first_inbound_text(session, ticket_id)

    try:
        snapshot = get_ticket_state(graph, ticket_id)
    except Exception:
        log.warning("the graph state of %s could not be read", ticket_id, exc_info=True)
        raise DatabaseError("the agent state could not be read", code="AGENT_UNAVAILABLE") from None

    if not snapshot.exists:
        if not text:
            raise ValidationFailed("this ticket has no customer message")
        first_input = {"ticket_id": ticket_id, "source": source_of(channel), "text": text}
        return RunPlan(mode="start", customer_email=customer_email, graph_input=first_input)
    if snapshot.finished:
        return RunPlan(mode="finished", customer_email=customer_email)
    if snapshot.pending_interrupt is not None:
        pending = snapshot.pending_interrupt
        approval_id = pending.get("approval_id")
        approval = get_approval(factory, approval_id) if isinstance(approval_id, int) else None
        if approval is None or approval["status"] == "pending":
            return RunPlan(mode="waiting", customer_email=customer_email, pending=pending)
        # decided (or expired) but the graph did not resume: do it now. The graph reads the decision from the
        # approvals table, so the resume value is only a marker.
        return RunPlan(
            mode="resume",
            customer_email=customer_email,
            graph_input=Command(resume={"status": approval["status"]}),
            pending=pending,
        )
    return RunPlan(mode="continue", customer_email=customer_email, graph_input=None)


def parent_of(namespace: tuple[str, ...]) -> str | None:
    """"run_support:3f2a..." -> "run_support". Empty for a node of the top graph."""
    return namespace[-1].split(":")[0] if namespace else None


def make_work(
    *,
    plan: RunPlan,
    graph: Any,
    ticket_id: str,
    ctx: RunContext,
    factory: sessionmaker[Session],
    acquired: bool,
) -> Work:
    """The function the worker thread runs. It sets the run context, streams the graph, and settles the ticket status."""

    def run_graph(emit: Emit) -> dict[str, Any]:
        token = ctx_var.set(ctx)  # set in THIS thread, once, and reset at the end
        seen: dict[str, Any] = {}
        interrupted = False
        started = last = time.perf_counter()
        try:
            with bind_context(ticket_id=ticket_id, thread_id=ticket_id):
                mark_working(factory, ticket_id)
                audit("agent_run_started", mode=plan.mode)
                emit("start", {"ticket_id": ticket_id, "mode": plan.mode})

                config = {
                    "configurable": {"thread_id": ticket_id},  # thread_id = ticket id, so the checkpointer finds the run
                    "metadata": {"ticket_id": ticket_id, "role": ctx.actor_role, "env": settings.env},
                    "tags": [settings.env],
                }
                for namespace, chunk in graph.stream(plan.graph_input, config, stream_mode="updates", subgraphs=True):
                    for node, update in chunk.items():
                        if node == "__interrupt__":
                            if not interrupted:  # the same interrupt is reported by the subgraph and by its parent
                                interrupted = True
                                emit("interrupt", interrupt_payload(update))
                            continue
                        if node == "triage" and isinstance(update, dict):
                            seen["intent"], seen["order_ref"] = update.get("intent"), update.get("order_ref")
                        now = time.perf_counter()
                        detail = summarize_update(update)
                        emit(
                            "node",
                            {
                                "node": node,
                                "parent": parent_of(namespace),
                                "status": "done",
                                "ms": round((now - last) * 1000),
                                "summary": one_line(detail),
                                "detail": detail,
                            },
                        )
                        last = now

                try:
                    waiting = get_ticket_state(graph, ticket_id).pending_interrupt is not None
                except Exception:
                    log.warning("the graph state could not be read after the run", exc_info=True)
                    waiting = interrupted
                status = settle_ticket(
                    factory, ticket_id, waiting=waiting, intent=seen.get("intent"), order_ref=seen.get("order_ref")
                )
                audit("agent_run_finished", ticket_status=status, waiting=waiting)
                return {
                    "status": "waiting_approval" if waiting else "finished",
                    "ticket_status": status,
                    "ms": round((time.perf_counter() - started) * 1000),
                }
        finally:
            ctx_var.reset(token)

    def work(emit: Emit) -> dict[str, Any]:
        try:
            if plan.mode == "finished":
                return {"status": "finished", "ticket_status": ticket_status(factory, ticket_id), "note": "already processed"}
            if plan.mode == "waiting":
                emit("interrupt", interrupt_payload(plan.pending))
                return {"status": "waiting_approval", "ticket_status": ticket_status(factory, ticket_id)}
            return run_graph(emit)
        finally:
            if acquired:
                ACTIVE.release(ticket_id)

    return work


@router.post("/{ticket_id}/run")
async def run_ticket(
    ticket_id: str,
    user: SupportUser,
    factory: SessionFactoryDep,
    shop: ShopDep,
    graph: GraphDep,
    limits: LimitsDep,
    clock: ClockDep,
) -> StreamingResponse:
    plan = await run_in_threadpool(prepare_run, factory, graph, ticket_id)
    ctx = build_run_context(
        shop=shop,
        factory=factory,
        ticket_id=ticket_id,
        customer_email=plan.customer_email,
        user=user,
        limits=limits,
        clock=clock,
    )

    needs_run = plan.mode in ("start", "continue", "resume")
    if needs_run and not ACTIVE.acquire(ticket_id):
        raise IdempotencyConflict("a run is already in progress for this ticket")

    work = make_work(plan=plan, graph=graph, ticket_id=ticket_id, ctx=ctx, factory=factory, acquired=needs_run)
    try:
        stream = EventStream(work)
    except Exception:
        if needs_run:
            ACTIVE.release(ticket_id)  # the worker never started, so nobody else would release it
        raise
    return StreamingResponse(stream.events(), media_type="text/event-stream", headers=SSE_HEADERS)
