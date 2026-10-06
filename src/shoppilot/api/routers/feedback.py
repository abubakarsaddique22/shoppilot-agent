"""Staff feedback (Step Q): a thumbs up or down on a ticket, with an optional note.

    POST /v1/feedback   {"ticket_id", "rating": "up" | "down", "note": "", "run_id": null}   support and above

The feedback is written to audit_log (event "feedback"), so it is never lost. When a LangSmith run id is given and
tracing is on, the same rating is also attached to that run as feedback "staff_rating" (1.0 up, 0.0 down). That is how a
bad run becomes a new evaluation case later (Step V). A LangSmith problem never fails the request.

The note is written by staff, not by the customer. The customer message is never copied into the audit row.
"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from shoppilot.api.deps import SessionFactoryDep, SupportUser, write_audit
from shoppilot.core.config import settings
from shoppilot.core.errors import TicketNotFound
from shoppilot.core.logging import get_logger
from shoppilot.db.models import TicketRow

log = get_logger(__name__)
router = APIRouter(prefix="/v1/feedback", tags=["feedback"])

FeedbackNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class FeedbackIn(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=50)
    rating: Literal["up", "down"]
    note: FeedbackNote = ""
    run_id: UUID | None = None  # the LangSmith run, when the UI knows it


class FeedbackOut(BaseModel):
    ok: bool = True
    linked_to_langsmith: bool = False


def send_to_langsmith(run_id: UUID, rating: str, note: str) -> bool:
    """Attach the rating to the LangSmith run. Returns False (and logs) on any problem."""
    if not settings.langsmith_tracing:
        return False
    try:
        from langsmith import Client

        Client().create_feedback(
            run_id=run_id, key="staff_rating", score=1.0 if rating == "up" else 0.0, comment=note or None
        )
        return True
    except Exception:
        log.warning("feedback could not be sent to LangSmith", exc_info=True)
        return False


@router.post("")
def give_feedback(body: FeedbackIn, user: SupportUser, factory: SessionFactoryDep) -> FeedbackOut:
    with factory() as session:
        exists = session.scalar(select(TicketRow.id).where(TicketRow.id == body.ticket_id))
    if exists is None:
        raise TicketNotFound(f"ticket {body.ticket_id} not found", details={"ticket_id": body.ticket_id})

    linked = send_to_langsmith(body.run_id, body.rating, body.note) if body.run_id else False
    write_audit(
        factory, user.id, "feedback",
        ticket_id=body.ticket_id, rating=body.rating, note=body.note, run_id=str(body.run_id) if body.run_id else None,
    )  # fmt: skip
    return FeedbackOut(linked_to_langsmith=linked)
