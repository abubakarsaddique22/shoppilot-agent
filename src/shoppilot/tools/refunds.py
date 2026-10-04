"""Refund tools (Step I): propose_refund (no side effect) and issue_refund (moves money).

The model proposes, the policy engine decides. issue_refund runs the engine again as the last line of defence,
checks the human approval for manager and owner tiers, and is safe to retry: the same idempotency_key
returns the earlier result and never refunds twice.

Approval rule (the approvals service of Step M must follow it): an approval row counts when status is "approved",
it belongs to this ticket, its tier is at least the tier the engine asks for, and payload_json["amount_pkr"]
(the amount the manager approved, maybe edited downwards) covers the refund.
"""
from typing import Annotated, Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select

from shoppilot.db.models import ActionRow, ApprovalRow
from shoppilot.policy.refund_rules import RefundDecision, evaluate_refund
from shoppilot.shop.base import Order
from shoppilot.tools.context import RunContext, audit, find_action, get_ctx, record_action, tool_guard
from shoppilot.tools.orders import load_own_order

TIER_RANK = {"manager": 1, "owner": 2}
Reason = Literal["late", "damaged", "wrong_item", "not_as_described", "other"]
Evidence = list[Annotated[str, Field(max_length=300)]]


class ProposeRefundArgs(BaseModel):
    order_id: str
    amount_pkr: int = Field(gt=0, le=100_000)
    reason: Reason
    evidence: Evidence = Field(default_factory=list, max_length=5)


class IssueRefundArgs(ProposeRefundArgs):
    idempotency_key: str = Field(min_length=3, max_length=120)  # e.g. "T-1042:88731:refund"
    approval_id: int | None = None


def _auto_refunded_today(ctx: RunContext) -> int:
    """PKR already refunded automatically today in the whole store (for the daily cap)."""
    start = ctx.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with ctx.session_factory() as session:
        rows = session.scalars(
            select(ActionRow).where(ActionRow.tool == "issue_refund", ActionRow.created_at >= start)
        ).all()
    return sum(
        r.args_json.get("amount_pkr", 0)
        for r in rows
        if r.args_json.get("tier") == "auto" and (r.result_json or {}).get("ok")
    )


def _ruling(
    ctx: RunContext, order_id: str, amount_pkr: int, reason: str, evidence: list[str] | None
) -> tuple[Order, RefundDecision]:
    order = load_own_order(order_id)
    decision = evaluate_refund(
        order.model_dump(),
        amount_pkr,
        reason,
        evidence or [],
        ctx.now(),
        limits=ctx.limits,
        refunded_today_pkr=_auto_refunded_today(ctx),
    )
    return order, decision


def _approval_ok(ctx: RunContext, approval_id: int | None, tier: str, amount_pkr: int) -> bool:
    if approval_id is None:
        return False
    with ctx.session_factory() as session:
        row = session.get(ApprovalRow, approval_id)
    return (
        row is not None
        and row.status == "approved"
        and row.ticket_id == ctx.ticket_id
        and TIER_RANK.get(row.tier, 0) >= TIER_RANK[tier]
        and row.payload_json.get("amount_pkr", 0) >= amount_pkr
    )


@tool("propose_refund", args_schema=ProposeRefundArgs)
@tool_guard("read")
def propose_refund(order_id: str, amount_pkr: int, reason: str, evidence: list[str] | None = None) -> dict[str, Any]:
    """Ask the policy engine what would happen to a refund: tier (auto, manager, owner, deny), allowed amount, reasons and policy sections. No money moves."""
    _, decision = _ruling(get_ctx(), order_id, amount_pkr, reason, evidence)
    return {"ok": True, **decision.model_dump()}


@tool("issue_refund", args_schema=IssueRefundArgs)
@tool_guard("write")
def issue_refund(
    order_id: str,
    amount_pkr: int,
    reason: str,
    idempotency_key: str,
    evidence: list[str] | None = None,
    approval_id: int | None = None,
) -> dict[str, Any]:
    """Refund an order. The policy is checked again here. Manager and owner tier refunds need an approved approval_id. Retrying with the same idempotency_key never refunds twice."""
    earlier = find_action(idempotency_key)
    if earlier is not None:
        return earlier

    ctx = get_ctx()
    order, decision = _ruling(ctx, order_id, amount_pkr, reason, evidence)
    if decision.tier == "deny" or amount_pkr > decision.allowed_amount:
        return {"ok": False, "error": "POLICY_DENIED", "reasons": decision.reasons, "policy_refs": decision.policy_refs}
    if decision.tier in TIER_RANK and not _approval_ok(ctx, approval_id, decision.tier, amount_pkr):
        return {"ok": False, "error": "APPROVAL_REQUIRED", "tier": decision.tier}

    refund = ctx.shop.create_refund(order.id, amount_pkr, key=idempotency_key, reason=reason)
    result = {
        "ok": True,
        "refund_id": refund.id,
        "order_id": refund.order_id,
        "amount_pkr": refund.amount_pkr,
        "status": refund.status,
    }
    detail = {"order_id": order.id, "amount_pkr": amount_pkr, "tier": decision.tier, "approval_id": approval_id}
    record_action("issue_refund", detail, result, idempotency_key)
    audit("refund_issued", **detail)
    return result
