"""Small helpers shared by several routers (Step Q): ticket ids, creating a ticket, and the ticket status after a run.

Keeping them here means the routers stay thin and never import each other.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.api.schemas import TicketCreated
from shoppilot.core.errors import DatabaseError, TicketNotFound
from shoppilot.db.models import MessageRow, TicketRow

FIRST_TICKET_NUMBER = 1001
DUPLICATE_WINDOW = timedelta(minutes=10)
_TICKET_NUMBER = re.compile(r"^T-(\d+)$")

# Where did the item come from? This decides which agents the supervisor may pick (agents/supervisor.py ALLOWED_ROUTES).
# Every ticket is a customer message, which is untrusted text, so it can only ever reach the support agent, whatever
# the text says. A staff "ui_command" source (inventory, listing, reports from a text box) needs its own endpoint.
SOURCE_BY_CHANNEL: dict[str, str] = {"email": "customer_email", "simulator": "customer_email", "ui": "customer_email"}


def source_of(channel: str) -> str:
    return SOURCE_BY_CHANNEL.get(channel, "customer_email")


def events_url(ticket_id: str) -> str:
    return f"/v1/tickets/{ticket_id}/run"


def created_response(ticket_id: str, status: str) -> TicketCreated:
    return TicketCreated(ticket_id=ticket_id, status=status, events_url=events_url(ticket_id))


# ------------------------------------------------------------------ create
def next_ticket_id(session: Session) -> str:
    """T-1001, T-1002 ... Ids that do not look like T-<number> (T-demo-1, report-2026-10-06) are ignored."""
    ids = session.scalars(select(TicketRow.id).where(TicketRow.id.like("T-%"))).all()
    numbers = [int(m.group(1)) for ticket_id in ids if (m := _TICKET_NUMBER.match(ticket_id))]
    return f"T-{max(numbers, default=FIRST_TICKET_NUMBER - 1) + 1}"


def create_ticket(
    factory: sessionmaker[Session], *, channel: str, customer_email: str, subject: str, body: str
) -> str:
    """Create the ticket and its first inbound message. Returns the ticket id.

    The email is stored in lower case, because the tools compare it with the order email in lower case.
    Two requests at the same moment can pick the same number: the primary key refuses the second one, so we try again.
    """
    email = customer_email.strip().lower()
    for _attempt in range(3):
        with factory() as session:
            ticket_id = next_ticket_id(session)
            session.add(
                TicketRow(id=ticket_id, channel=channel, customer_email=email, subject=subject.strip(), status="new")
            )
            session.flush()  # the ticket must exist before the message that points to it
            session.add(MessageRow(ticket_id=ticket_id, direction="inbound", body=body))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            return ticket_id
    raise DatabaseError("the ticket could not be created, please try again", code="TICKET_ID_CONFLICT")


def find_recent_duplicate(
    factory: sessionmaker[Session], *, customer_email: str, body: str, now: datetime
) -> tuple[str, str] | None:
    """(ticket id, status) of an email ticket with the same sender and text from the last 10 minutes, or None.
    A mail service that delivers the same webhook twice must not open two tickets (blueprint threat: double delivery)."""
    with factory() as session:
        row = session.execute(
            select(TicketRow.id, TicketRow.status)
            .join(MessageRow, MessageRow.ticket_id == TicketRow.id)
            .where(
                TicketRow.channel == "email",
                TicketRow.customer_email == customer_email.strip().lower(),
                MessageRow.direction == "inbound",
                MessageRow.body == body,
                TicketRow.created_at >= now - DUPLICATE_WINDOW,
            )
            .order_by(TicketRow.created_at.desc())
            .limit(1)
        ).first()
    return (str(row[0]), str(row[1])) if row else None


# ------------------------------------------------------------------ read
def first_inbound_text(session: Session, ticket_id: str) -> str | None:
    return session.scalar(
        select(MessageRow.body)
        .where(MessageRow.ticket_id == ticket_id, MessageRow.direction == "inbound")
        .order_by(MessageRow.id)
        .limit(1)
    )


def ticket_status(factory: sessionmaker[Session], ticket_id: str) -> str:
    with factory() as session:
        ticket = session.get(TicketRow, ticket_id)
        if ticket is None:
            raise TicketNotFound(f"ticket {ticket_id} not found", details={"ticket_id": ticket_id})
        return ticket.status


# ------------------------------------------------------------------ status changes around a run
def mark_working(factory: sessionmaker[Session], ticket_id: str) -> None:
    """A run starts or continues. An escalated ticket keeps its status."""
    with factory() as session:
        ticket = session.get(TicketRow, ticket_id)
        if ticket is not None and ticket.status != "escalated":
            ticket.status = "working"
            session.commit()


def settle_ticket(
    factory: sessionmaker[Session],
    ticket_id: str,
    *,
    waiting: bool,
    intent: str | None = None,
    order_ref: str | None = None,
) -> str:
    """The run stopped. Waiting for a human -> waiting_approval. Otherwise done, unless the agent escalated the ticket
    (escalate_to_human already set that). intent and order_ref are saved when the run saw them. Returns the new status."""
    with factory() as session:
        ticket = session.get(TicketRow, ticket_id)
        if ticket is None:
            raise TicketNotFound(f"ticket {ticket_id} not found", details={"ticket_id": ticket_id})
        if waiting:
            ticket.status = "waiting_approval"
        elif ticket.status != "escalated":
            ticket.status = "done"
        if intent:
            ticket.intent = intent
        if order_ref:
            ticket.order_ref = order_ref
        session.commit()
        return ticket.status
