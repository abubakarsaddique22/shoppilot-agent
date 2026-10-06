"""Tickets (Step Q): create, list, detail.

POST /v1/tickets        support and above: a new ticket with its first customer message (status "new")
GET  /v1/tickets        every role: the inbox, with filters and paging
GET  /v1/tickets/{id}   every role: messages, actions, and where the agent run stands

Running the agent is in runs.py. The message bodies are untrusted customer text: they are stored and returned as plain
text, and the UI must show them with textContent, never as HTML.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from shoppilot.agents.checkpoint import HistoryItem, TicketSnapshot, get_ticket_history, get_ticket_state
from shoppilot.api.deps import DbDep, OptionalGraphDep, ReaderUser, SessionFactoryDep, SupportUser, write_audit
from shoppilot.api.schemas import ActionOut, MessageOut, TicketCreate, TicketCreated, TicketDetail, TicketOut
from shoppilot.api.services import create_ticket, created_response
from shoppilot.core.errors import TicketNotFound
from shoppilot.core.logging import get_logger
from shoppilot.db.models import ActionRow, MessageRow, TicketRow

log = get_logger(__name__)
router = APIRouter(prefix="/v1/tickets", tags=["tickets"])

TicketStatus = Literal["new", "working", "waiting_approval", "done", "escalated"]


@router.post("", status_code=201)
def create(body: TicketCreate, user: SupportUser, factory: SessionFactoryDep) -> TicketCreated:
    ticket_id = create_ticket(
        factory, channel="ui", customer_email=body.customer_email, subject=body.subject, body=body.body
    )
    write_audit(factory, user.id, "ticket_created", ticket_id=ticket_id, channel="ui")
    return created_response(ticket_id, "new")


@router.get("")
def list_tickets(
    user: ReaderUser,
    db: DbDep,
    status: Annotated[TicketStatus | None, Query()] = None,
    intent: Annotated[str | None, Query(max_length=50)] = None,
    channel: Annotated[str | None, Query(max_length=30)] = None,
    since: Annotated[datetime | None, Query(description="only tickets created at or after this time")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TicketOut]:
    """Newest first. Tickets of the scheduler (daily report, low-stock check) are hidden unless channel=scheduler."""
    stmt = select(TicketRow).order_by(TicketRow.created_at.desc(), TicketRow.id.desc())
    stmt = stmt.where(TicketRow.channel == channel) if channel else stmt.where(TicketRow.channel != "scheduler")
    if status:
        stmt = stmt.where(TicketRow.status == status)
    if intent:
        stmt = stmt.where(TicketRow.intent == intent)
    if since:
        # the database holds naive UTC times; a client may send a time zone
        since_utc = since.astimezone(UTC).replace(tzinfo=None) if since.tzinfo else since
        stmt = stmt.where(TicketRow.created_at >= since_utc)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [TicketOut.model_validate(row) for row in rows]


@router.get("/{ticket_id}")
def ticket_detail(ticket_id: str, user: ReaderUser, db: DbDep, graph: OptionalGraphDep) -> TicketDetail:
    ticket = db.get(TicketRow, ticket_id)
    if ticket is None:
        raise TicketNotFound(f"ticket {ticket_id} not found", details={"ticket_id": ticket_id})

    messages = db.scalars(select(MessageRow).where(MessageRow.ticket_id == ticket_id).order_by(MessageRow.id)).all()
    actions = db.scalars(select(ActionRow).where(ActionRow.ticket_id == ticket_id).order_by(ActionRow.id)).all()

    snapshot: TicketSnapshot | None = None
    history: list[HistoryItem] = []
    if graph is not None:
        try:
            snapshot = get_ticket_state(graph, ticket_id)
            history = get_ticket_history(graph, ticket_id)
        except Exception:  # the detail page still works when the checkpointer has a problem
            log.warning("the graph state of %s could not be read", ticket_id, exc_info=True)
            snapshot, history = None, []
        else:
            if snapshot.intent is None and ticket.intent:  # the supervisor state does not hold the intent
                snapshot = snapshot.model_copy(update={"intent": ticket.intent})

    return TicketDetail(
        ticket=TicketOut.model_validate(ticket),
        messages=[MessageOut.model_validate(m) for m in messages],
        actions=[ActionOut.model_validate(a) for a in actions],
        graph=snapshot,
        history=history,
    )
