"""Approval inbox and decisions (Step Q, using the approvals service of Step M).

    GET  /v1/approvals?status=pending     manager and above: the inbox (a manager sees the manager tier, an owner both)
    GET  /v1/approvals/{id}               one approval, same visibility rule
    POST /v1/approvals/{id}/decision      approve, reject or lower the amount; then the paused graph resumes

Rules this file enforces (and the tools enforce again, see issue_refund):
- Who may DECIDE a tier comes from APPROVER_ROLES in deps.py: manager tier -> manager or owner, owner tier -> owner only.
  Admin can read the inbox but can NOT decide: a technical role must not move money.
- The decision is written to the approvals TABLE first (with a mandatory note and an audit row). Only then the graph is
  resumed. The resume value is just a marker: the graph reads the real decision from the table, so text in an email or
  in a request can never approve anything.
- A second decision on the same approval gives 409. An expired approval gives 422.
- If the graph cannot be resumed right now (no checkpointer, a run is in progress, the model failed), the decision is
  still recorded and the answer says graph_status "recorded". Running the ticket again (POST /v1/tickets/{id}/run) sees
  the decided approval and carries on, so nothing is lost.
- Purchase-order approvals (action != refund) have no paused customer ticket: they are only recorded.

The graph code is synchronous, so these endpoints are plain `def` functions: FastAPI runs them in a worker thread, and
the run context (ctx_var) can be set and reset inside the same thread.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from langgraph.types import Command
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.agents.checkpoint import get_ticket_state
from shoppilot.api.deps import (
    APPROVER_ROLES,
    ClockDep,
    DbDep,
    LimitsDep,
    ManagerUser,
    OptionalGraphDep,
    SessionFactoryDep,
    ShopDep,
    build_run_context,
    visible_tiers,
    write_audit,
)
from shoppilot.api.routers.runs import ACTIVE
from shoppilot.api.schemas import ApprovalOut, DecisionIn, DecisionOut, UserOut
from shoppilot.api.services import events_url, settle_ticket
from shoppilot.approvals.service import record_decision
from shoppilot.core.config import settings
from shoppilot.core.errors import NotFound, PermissionDenied
from shoppilot.core.logging import bind_context, get_logger
from shoppilot.db.models import ApprovalRow, TicketRow
from shoppilot.policy.limits import Limits
from shoppilot.tools.context import audit, ctx_var

log = get_logger(__name__)
router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]


def to_out(row: ApprovalRow, now: datetime) -> ApprovalOut:
    """A pending approval whose time is over is shown as expired, even before a sweep job has changed the row."""
    out = ApprovalOut.model_validate(row)
    if out.status == "pending" and row.expires_at <= now:
        out = out.model_copy(update={"status": "expired"})
    return out


def not_found(approval_id: int) -> NotFound:
    return NotFound(f"approval {approval_id} not found", code="APPROVAL_NOT_FOUND")


# ------------------------------------------------------------------ inbox
@router.get("")
def list_approvals(
    user: ManagerUser,
    db: DbDep,
    clock: ClockDep,
    status: Annotated[ApprovalStatus, Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ApprovalOut]:
    """Pending approvals come oldest first (the one waiting longest is on top), the others newest first."""
    now = clock()
    stmt = select(ApprovalRow).where(ApprovalRow.tier.in_(visible_tiers(user.role)))
    if status == "pending":
        stmt = stmt.where(ApprovalRow.status == "pending", ApprovalRow.expires_at > now)
        stmt = stmt.order_by(ApprovalRow.requested_at, ApprovalRow.id)
    else:
        if status == "expired":
            stmt = stmt.where(
                or_(ApprovalRow.status == "expired", and_(ApprovalRow.status == "pending", ApprovalRow.expires_at <= now))
            )
        else:
            stmt = stmt.where(ApprovalRow.status == status)
        stmt = stmt.order_by(ApprovalRow.requested_at.desc(), ApprovalRow.id.desc())
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [to_out(row, now) for row in rows]


@router.get("/{approval_id}")
def get_one(approval_id: int, user: ManagerUser, db: DbDep, clock: ClockDep) -> ApprovalOut:
    row = db.get(ApprovalRow, approval_id)
    if row is None or row.tier not in visible_tiers(user.role):  # a tier you cannot see looks like "not found"
        raise not_found(approval_id)
    return to_out(row, clock())


# ------------------------------------------------------------------ decision
def resume_after_decision(
    *,
    graph: Any,
    factory: sessionmaker[Session],
    shop: Any,
    limits: Limits,
    clock: Any,
    user: UserOut,
    ticket_id: str,
    approval_id: int,
    status: str,
) -> bool:
    """Resume the paused graph of the ticket. Returns True when it ran on, False when it was not possible right now.

    It only resumes when the graph is really waiting on THIS approval, and only when no other run holds the ticket.
    The decision itself is already saved, so a False here loses nothing.
    """
    if graph is None:
        return False
    try:
        pending = get_ticket_state(graph, ticket_id).pending_interrupt
    except Exception:
        log.warning("the graph state of %s could not be read", ticket_id, exc_info=True)
        return False
    if pending is None or pending.get("approval_id") != approval_id:
        return False
    if not ACTIVE.acquire(ticket_id):  # a run is going on for this ticket: it will see the decision itself
        return False

    try:
        with factory() as session:
            ticket = session.get(TicketRow, ticket_id)
            customer_email = ticket.customer_email if ticket is not None else ""
        ctx = build_run_context(
            shop=shop, factory=factory, ticket_id=ticket_id, customer_email=customer_email,
            user=user, limits=limits, clock=clock,
        )  # fmt: skip
        token = ctx_var.set(ctx)  # set in THIS thread and reset below
        try:
            with bind_context(ticket_id=ticket_id, thread_id=ticket_id):
                audit("agent_run_started", mode="resume", approval_id=approval_id)
                config = {
                    "configurable": {"thread_id": ticket_id},
                    "metadata": {"ticket_id": ticket_id, "role": user.role, "env": settings.env},
                    "tags": [settings.env],
                }
                graph.invoke(Command(resume={"status": status}), config)  # marker only: the graph reads the table
                try:
                    waiting = get_ticket_state(graph, ticket_id).pending_interrupt is not None
                except Exception:
                    log.warning("the graph state could not be read after the resume", exc_info=True)
                    waiting = False
                ticket_status = settle_ticket(factory, ticket_id, waiting=waiting)
                audit("agent_run_finished", ticket_status=ticket_status, waiting=waiting)
        finally:
            ctx_var.reset(token)
        return True
    except Exception:
        # The decision is saved. The ticket stays "working" and POST /run continues it from the checkpoint.
        log.warning("the graph of %s could not be resumed after the decision", ticket_id, exc_info=True)
        return False
    finally:
        ACTIVE.release(ticket_id)


@router.post("/{approval_id}/decision")
def decide(
    approval_id: int,
    body: DecisionIn,
    user: ManagerUser,
    factory: SessionFactoryDep,
    shop: ShopDep,
    graph: OptionalGraphDep,
    limits: LimitsDep,
    clock: ClockDep,
) -> DecisionOut:
    with factory() as session:
        row = session.get(ApprovalRow, approval_id)
        if row is None:
            raise not_found(approval_id)
        tier, ticket_id, action = row.tier, row.ticket_id, row.action

    if user.role not in APPROVER_ROLES.get(tier, frozenset()):
        write_audit(factory, user.id, "approval_forbidden", approval_id=approval_id, tier=tier, role=user.role)
        raise PermissionDenied(f"role {user.role} may not decide a {tier}-tier approval")

    # The service checks: still pending, not expired, mandatory note, amount only lower. A second decision gives 409.
    record_decision(
        factory, approval_id, status=body.status, decided_by=user.email, note=body.note,
        amount_pkr=body.amount_pkr, now=clock(),
    )  # fmt: skip
    write_audit(
        factory, user.id, "approval_decided",
        approval_id=approval_id, ticket_id=ticket_id, tier=tier, status=body.status, amount_pkr=body.amount_pkr,
    )  # fmt: skip

    resumed = False
    if action == "refund":
        resumed = resume_after_decision(
            graph=graph, factory=factory, shop=shop, limits=limits, clock=clock, user=user,
            ticket_id=ticket_id, approval_id=approval_id, status=body.status,
        )  # fmt: skip
    return DecisionOut(
        approval_id=approval_id,
        ticket_id=ticket_id,
        graph_status="resumed" if resumed else "recorded",
        events_url=events_url(ticket_id) if action == "refund" else None,
    )
