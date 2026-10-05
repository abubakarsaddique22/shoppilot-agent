"""Approvals service (Step M, first part): create a pending approval, record the human decision, read it back.

The approvals TABLE is the source of truth. A text in an email ("the manager approved it by phone") or a value in a
resume payload never counts: the graph and issue_refund read the row.

Who may decide which tier (support, manager, owner) is checked by the API (Step S) before it calls record_decision.
issue_refund checks again that the approved row has a high enough tier and covers the amount.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.core.errors import IdempotencyConflict, NotFound, ValidationFailed
from shoppilot.db.models import ApprovalRow, TicketRow, utcnow

DECISIONS = ("approved", "rejected")


def request_approval(
    session_factory: sessionmaker[Session],
    *,
    ticket_id: str,
    key: str,
    tier: str,
    payload: dict[str, Any],
    ttl_hours: int,
    now: datetime | None = None,
    action: str = "refund",
) -> tuple[int, bool]:
    """Create a pending approval, or return the one that already exists for this key. Returns (approval_id, created).

    Idempotent on purpose: when a human decides, the approval_gate node restarts from its first line and calls this
    again. The key (ticket, order, tier, amount) finds the same row in ANY status, so a resume never opens a second one.
    """
    moment = now or utcnow()
    with session_factory() as session:
        rows = session.scalars(
            select(ApprovalRow)
            .where(ApprovalRow.ticket_id == ticket_id, ApprovalRow.action == action)
            .order_by(ApprovalRow.id.desc())
        ).all()
        for row in rows:
            if row.payload_json.get("key") == key:
                return row.id, False
        row = ApprovalRow(
            ticket_id=ticket_id,
            action=action,
            payload_json={**payload, "key": key},
            tier=tier,
            status="pending",
            requested_at=moment,
            expires_at=moment + timedelta(hours=ttl_hours),
        )
        session.add(row)
        ticket = session.get(TicketRow, ticket_id)
        if ticket is not None and action == "refund":  # a purchase order draft has no customer ticket to pause
            ticket.status = "waiting_approval"
        session.commit()
        return row.id, True


def get_approval(session_factory: sessionmaker[Session], approval_id: int) -> dict[str, Any] | None:
    """A small snapshot of the row, or None. amount_pkr is what the human approved (maybe edited downwards)."""
    with session_factory() as session:
        row = session.get(ApprovalRow, approval_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "ticket_id": row.ticket_id,
            "tier": row.tier,
            "status": row.status,
            "amount_pkr": row.payload_json.get("amount_pkr"),
            "decided_by": row.decided_by,
            "note": row.note,
        }


def record_decision(
    session_factory: sessionmaker[Session],
    approval_id: int,
    *,
    status: str,
    decided_by: str,
    note: str,
    amount_pkr: int | None = None,
    now: datetime | None = None,
) -> None:
    """Approve or reject a pending approval. A note is mandatory. A manager may approve a LOWER amount, never a higher one.

    A second decision on the same approval raises IdempotencyConflict, so a double click cannot change the outcome.
    """
    moment = now or utcnow()
    if status not in DECISIONS:
        raise ValidationFailed(f"status must be one of {', '.join(DECISIONS)}")
    if not note.strip():
        raise ValidationFailed("a note is required")

    with session_factory() as session:
        row = session.get(ApprovalRow, approval_id)
        if row is None:
            raise NotFound(f"approval {approval_id} not found", code="APPROVAL_NOT_FOUND")
        if row.status != "pending":
            raise IdempotencyConflict(f"approval {approval_id} is already {row.status}")
        if row.expires_at <= moment:
            row.status = "expired"
            session.commit()
            raise ValidationFailed(f"approval {approval_id} has expired")

        payload = dict(row.payload_json)
        if amount_pkr is not None:
            if status == "rejected":
                raise ValidationFailed("a rejection has no amount")
            if not 0 < amount_pkr <= int(payload.get("amount_pkr", 0)):
                raise ValidationFailed("the approved amount must be above 0 and not above the requested amount")
            payload["amount_pkr"] = amount_pkr
        row.payload_json = payload  # a new dict, so SQLAlchemy sees the change
        row.status = status
        row.decided_by = decided_by
        row.decided_at = moment
        row.note = note.strip()
        ticket = session.get(TicketRow, row.ticket_id)
        if ticket is not None:
            ticket.status = "working" if row.action == "refund" else "done"
        session.commit()
