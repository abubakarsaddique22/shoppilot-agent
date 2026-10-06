"""Customer email and escalation tools (Step I).

send_customer_email: the recipient is always the customer of the ticket (never an argument). The text comes from a
fixed template plus short fields; fields may not contain email addresses or links (guardrails.validators). For now the
email is saved as an outbound message on the ticket (the UI shows it); sending it through SMTP or SES is added with the
scheduled jobs (Step P).
escalate_to_human: the safe exit. No budget, no role check, always allowed.
"""
from string import Formatter
from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from shoppilot.core.errors import BudgetExceeded, ValidationFailed
from shoppilot.db.models import MessageRow, TicketRow
from shoppilot.guardrails.validators import MAX_FIELD_CHARS, check_plain_fields
from shoppilot.tools.context import audit, find_action, get_ctx, record_action, tool_guard

MAX_EMAILS_PER_TICKET = 3

TEMPLATES = {
    "status_update": "Hello,\n\nHere is the latest on your order {order_id}: {details}\n\nRegards,\nShopPilot support",
    "refund_confirmed": (
        "Hello,\n\nYour refund of PKR {amount_pkr} for order {order_id} has been issued. {details}\n\n"
        "Regards,\nShopPilot support"
    ),
    "refund_pending": (
        "Hello,\n\nYour refund request for order {order_id} is waiting for approval by our team. {details}\n\n"
        "Regards,\nShopPilot support"
    ),
    "refund_denied": (
        "Hello,\n\nWe are sorry, we could not refund order {order_id}. {details}\nPolicy: {policy_ref}\n\n"
        "Regards,\nShopPilot support"
    ),
    "need_verification": (
        "Hello,\n\nWe could not match that order to this email address. Please reply with your order number "
        "and the email address you used at checkout.\n\nRegards,\nShopPilot support"
    ),
    "general_reply": "Hello,\n\n{details}\n\nRegards,\nShopPilot support",
}


class SendEmailArgs(BaseModel):
    template: Literal[
        "status_update", "refund_confirmed", "refund_pending", "refund_denied", "need_verification", "general_reply"
    ]
    fields: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=3, max_length=120)


class EscalateArgs(BaseModel):
    summary: str = Field(min_length=10, max_length=1500)


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@tool("send_customer_email", args_schema=SendEmailArgs)
@tool_guard("write")
def send_customer_email(template: str, idempotency_key: str, fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Send a templated email to the customer of this ticket. Pick a template and fill its fields (order_id, amount_pkr, details, policy_ref). Fields are short plain text: no email addresses, no links."""
    earlier = find_action(idempotency_key)
    if earlier is not None:
        return earlier

    ctx = get_ctx()
    values = fields or {}
    text = TEMPLATES[template]
    needed = _placeholders(text)
    if needed - set(values):
        raise ValidationFailed(f"missing fields for template {template}: {sorted(needed - set(values))}")
    check_plain_fields(values, max_chars=MAX_FIELD_CHARS)

    body = text.format_map({name: values[name] for name in needed})
    with ctx.session_factory() as session:
        sent = session.scalar(
            select(func.count(MessageRow.id)).where(
                MessageRow.ticket_id == ctx.ticket_id, MessageRow.direction == "outbound"
            )
        )
        if (sent or 0) >= MAX_EMAILS_PER_TICKET:
            raise BudgetExceeded(
                f"email limit reached ({MAX_EMAILS_PER_TICKET} per ticket): escalate", code="EMAIL_LIMIT_REACHED"
            )
        session.add(MessageRow(ticket_id=ctx.ticket_id, direction="outbound", body=body))
        session.commit()

    result = {"ok": True, "status": "queued", "template": template}
    record_action("send_customer_email", {"template": template}, result, idempotency_key)
    audit("email_queued", template=template)
    return result


@tool("escalate_to_human", args_schema=EscalateArgs)
@tool_guard(None)
def escalate_to_human(summary: str) -> dict[str, Any]:
    """Hand the ticket to a human. Write one paragraph: what the customer wants, what you checked, why you cannot decide. Always allowed: use it whenever you are unsure."""
    ctx = get_ctx()
    with ctx.session_factory() as session:
        ticket = session.get(TicketRow, ctx.ticket_id)
        if ticket is not None:
            ticket.status = "escalated"
            session.commit()
    audit("ticket_escalated", summary=summary)
    return {"ok": True, "status": "escalated"}
