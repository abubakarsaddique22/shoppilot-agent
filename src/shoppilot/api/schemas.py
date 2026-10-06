"""Request and response shapes of the API (Step Q). Plain pydantic models, nothing else.

Every request body has size limits, because the text is untrusted and a very long text costs money (blueprint 13.2).
Responses built from a database row use `from_attributes`: `TicketOut.model_validate(row)`.

Not here on purpose: the password hash never appears in any response, and no response carries the customer
message of another ticket. The model never sees these shapes: they are only the HTTP side.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from shoppilot.agents.checkpoint import HistoryItem, TicketSnapshot

FROM_ROW = ConfigDict(from_attributes=True)  # build the model from a SQLAlchemy row

Email = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
MessageBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]  # a note is mandatory


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds
    email: str
    role: str


class UserOut(BaseModel):
    """Who is logged in. Read from the signed token, not from the request."""

    id: str
    email: str
    role: str


# ------------------------------------------------------------------ tickets
class TicketCreate(BaseModel):
    """A new ticket from the UI. The first customer message is the body. The channel is set by the server."""

    customer_email: Email
    subject: Subject = ""
    body: MessageBody


class TicketOut(BaseModel):
    model_config = FROM_ROW

    id: str
    channel: str
    customer_email: str
    subject: str
    status: str  # new | working | waiting_approval | done | escalated
    intent: str | None = None
    order_ref: str | None = None
    cost_pkr: float = 0
    tokens: int = 0
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """The body is untrusted customer text. The UI must show it with textContent, never as HTML."""

    model_config = FROM_ROW

    id: int
    direction: Literal["inbound", "outbound"]
    body: str
    created_at: datetime


class ActionOut(BaseModel):
    """One side effect the agent did (a refund, an email, a draft). Arguments are left out: the result is enough."""

    model_config = FROM_ROW

    id: int
    tool: str
    result_json: dict[str, Any] | None = None
    created_at: datetime


class TicketDetail(BaseModel):
    ticket: TicketOut
    messages: list[MessageOut]
    actions: list[ActionOut]
    graph: TicketSnapshot | None = None  # where the run stands; None when the graph is not available
    history: list[HistoryItem] = Field(default_factory=list)


# ------------------------------------------------------------------ approvals
class ApprovalOut(BaseModel):
    model_config = FROM_ROW

    id: int
    ticket_id: str
    action: str  # refund | purchase_order
    tier: Literal["manager", "owner"]
    status: Literal["pending", "approved", "rejected", "expired"]
    payload_json: dict[str, Any]  # amount, reasons, evidence summary
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None


class DecisionIn(BaseModel):
    """A manager or owner decides. The note is mandatory. An amount can only be LOWER than the request (the service checks)."""

    status: Literal["approved", "rejected"]
    note: Note
    amount_pkr: int | None = Field(default=None, gt=0, le=100_000)


class DecisionOut(BaseModel):
    approval_id: int
    ticket_id: str
    graph_status: Literal["resumed", "recorded"]  # recorded: no graph run was waiting (for example a purchase order)
    events_url: str | None = None


# ------------------------------------------------------------------ reports
class ReportOut(BaseModel):
    model_config = FROM_ROW

    id: int
    kind: str  # daily | low_stock
    s3_key: str | None = None
    created_at: datetime
    status: Literal["running", "done", "failed"]


class ReportRunOut(BaseModel):
    """The result of 'run the daily report now' (admin)."""

    status: Literal["done", "skipped"]
    location: str
    outcome: str


# ------------------------------------------------------------------ simulator and webhook
class SimulatorIn(BaseModel):
    """The demo form: pick a ready customer email. The server fills in a seeded customer and order."""

    preset: Literal["late_order", "damaged_item", "injection_attempt"]


class EmailWebhookIn(BaseModel):
    """An incoming customer email (a service sends it with the shared-secret header)."""

    from_email: Email
    subject: Subject = ""
    body: MessageBody


class TicketCreated(BaseModel):
    ticket_id: str
    status: str
    events_url: str  # POST here to start the agent run and read the live events
