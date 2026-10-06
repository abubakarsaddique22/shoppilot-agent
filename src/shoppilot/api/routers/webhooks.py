"""Incoming customer email (Step Q): a mail service posts it here with a shared secret.

    POST /v1/webhooks/email    header X-Webhook-Secret    {"from_email", "subject", "body"}

- The secret is compared in constant time. When SHOP_WEBHOOK_SECRET is empty the endpoint refuses everything, so a
  forgotten setting can never leave it open.
- The same email (same sender and text) delivered twice within 10 minutes opens ONE ticket. The second delivery gets
  200 with the first ticket id (blueprint threat "double delivery"); a new ticket gets 201.
- The ticket is created with status "new". It does NOT start the agent: a person (or a later worker) runs it from the
  inbox. That keeps this public endpoint cheap, and a flood of mails cannot spend model money by itself.
- The message body is untrusted text. It is stored as is and shown by the UI with textContent only.
- Plain `def`: the database code is synchronous, so FastAPI runs it in a worker thread and the event loop stays free.
"""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from shoppilot.api.deps import ClockDep, SessionFactoryDep, write_audit
from shoppilot.api.schemas import EmailWebhookIn, TicketCreated
from shoppilot.api.services import create_ticket, created_response, find_recent_duplicate
from shoppilot.core.config import settings
from shoppilot.core.errors import AuthError, ValidationFailed
from shoppilot.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 20_000  # the schema limits the text to 5000 characters; this refuses a huge body early


def check_secret(given: str | None) -> None:
    expected = settings.webhook_secret
    if not expected or not given or not hmac.compare_digest(given.encode(), expected.encode()):
        log.warning("webhook refused: missing or wrong secret")
        raise AuthError("the webhook secret is missing or wrong")


@router.post("/email", status_code=201)
def email_webhook(
    body: EmailWebhookIn,
    request: Request,
    response: Response,
    factory: SessionFactoryDep,
    clock: ClockDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
) -> TicketCreated:
    check_secret(x_webhook_secret)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise ValidationFailed("the request body is too large")

    duplicate = find_recent_duplicate(factory, customer_email=body.from_email, body=body.body, now=clock())
    if duplicate is not None:
        response.status_code = 200  # already known: nothing new was created
        return created_response(*duplicate)

    ticket_id = create_ticket(
        factory, channel="email", customer_email=body.from_email, subject=body.subject, body=body.body
    )
    write_audit(factory, "webhook", "ticket_created", ticket_id=ticket_id, channel="email")
    return created_response(ticket_id, "new")
