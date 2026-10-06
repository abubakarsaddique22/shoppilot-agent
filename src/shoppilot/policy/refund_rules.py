"""Refund rules (Step F). A pure function: no network, no database, no LLM.

The model may propose a refund. This function decides the tier and the allowed amount.
Amounts are whole PKR. `now` and order["delivered_at"] must both be naive or both timezone-aware.

Order dict keys: amount_paid, refunded_total, delivered_at (datetime or None), is_overdue, days_late,
customer_flagged, customer_blocked, refunds_last_90d, payment_method ("prepaid" or "cod"),
has_open_refund, non_refundable. Missing optional keys count as "no".
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shoppilot.policy.limits import Limits

Tier = Literal["auto", "manager", "owner", "deny"]

# Policy references use the heading path of configs/policies/*.md ("<document> > <section>").
REF_WINDOW = "Returns > Refund window"
REF_LATE = "Returns > Late deliveries"
REF_DAMAGED = "Returns > Damaged items"
REF_COD = "Returns > Cash on delivery refunds"
REF_LIMITS = "Returns > Refund approval limits"
REF_ALREADY = "Returns > Already refunded orders"
REF_NON_REFUNDABLE = "Returns > Non-refundable items"
REF_NOT_DUE = "Shipping > Delivery times"


class RefundDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: Tier
    allowed_amount: int
    reasons: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)


def _deny(reason: str, ref: str) -> RefundDecision:
    return RefundDecision(tier="deny", allowed_amount=0, reasons=[reason], policy_refs=[ref])


def _decide(tier: Tier, allowed: int, reasons: list[str], refs: list[str]) -> RefundDecision:
    """Build a decision. The limits section is always cited; duplicates are removed."""
    return RefundDecision(
        tier=tier, allowed_amount=allowed, reasons=reasons, policy_refs=list(dict.fromkeys(refs + [REF_LIMITS]))
    )


def evaluate_refund(
    order: Mapping[str, Any],
    requested_amount: int,
    reason: str,
    evidence: Sequence[str],
    now: datetime,
    *,
    limits: Limits | None = None,
    refunded_today_pkr: int = 0,
) -> RefundDecision:
    """Return the tier (auto, manager, owner, deny), the allowed amount, the reasons and the policy references."""
    cfg = limits or Limits()

    # --- hard invariants and outright denials -------------------------------------------------
    if isinstance(requested_amount, bool) or not isinstance(requested_amount, int) or requested_amount <= 0:
        return _deny("requested amount must be a positive whole number of PKR", REF_LIMITS)
    if order.get("non_refundable"):
        return _deny("item is in a non-refundable category", REF_NON_REFUNDABLE)

    refunded = order.get("refunded_total", 0)
    remaining = order["amount_paid"] - refunded
    if remaining <= 0:
        return _deny("order is already fully refunded", REF_ALREADY)
    if order.get("has_open_refund"):
        return _deny("a refund for this order is already open", REF_ALREADY)

    damaged = reason == "damaged"
    if damaged and not any(str(e).strip() for e in evidence):
        return _deny("damage claim needs evidence (photo or description)", REF_DAMAGED)

    # --- does the order qualify for a refund at all? -----------------------------------------
    delivered_at = order.get("delivered_at")
    days_late = order.get("days_late", 0)
    late_enough = days_late > cfg.late_threshold_days
    why: list[str] = []
    refs: list[str] = []

    if delivered_at is None:
        if not order.get("is_overdue"):
            return _deny("order is not yet due", REF_NOT_DUE)
        if not late_enough:
            return _deny(
                f"order is {days_late} days late; a refund needs more than {cfg.late_threshold_days}", REF_LATE
            )
    else:
        if delivered_at > now:
            return _deny("delivery date is in the future", REF_NOT_DUE)
        days_since = (now - delivered_at).days  # whole days: day 14 still counts as inside a 14-day window
        in_window = days_since <= cfg.refund_window_days
        if not (in_window or late_enough or damaged):
            return _deny("outside the refund window, not late and not damaged", REF_WINDOW)
        if in_window:
            why.append(f"delivered {days_since} days ago, within the {cfg.refund_window_days}-day window")
            refs.append(REF_WINDOW)
    if late_enough:
        why.append(f"order was {days_late} days late, more than {cfg.late_threshold_days}")
        refs.append(REF_LATE)
    if damaged:
        why.append("damage claim with evidence")
        refs.append(REF_DAMAGED)

    allowed = min(requested_amount, remaining)

    # --- owner tier ---------------------------------------------------------------------------
    owner: list[str] = []
    if order.get("customer_flagged") or order.get("customer_blocked"):
        owner.append("customer is flagged or blocked")
    repeats = order.get("refunds_last_90d", 0)
    if repeats >= cfg.repeat_refund_count:
        owner.append(f"customer already had {repeats} refunds in the last 90 days")
    if allowed > cfg.manager_limit_pkr:
        owner.append(f"amount {allowed} is above the manager limit {cfg.manager_limit_pkr}")
    if owner:
        return _decide("owner", allowed, why + owner, refs)

    # --- manager tier -------------------------------------------------------------------------
    manager: list[str] = []
    if allowed > cfg.auto_refund_limit_pkr:
        manager.append(f"amount {allowed} is above the automatic limit {cfg.auto_refund_limit_pkr}")
    method = order.get("payment_method")
    if method == "cod":
        manager.append("cash on delivery: staff must handle the payout")
        refs.append(REF_COD)
    elif method != "prepaid":
        manager.append("payment method is unknown or not prepaid")
    if damaged:
        manager.append("damage claim needs manager review")
    if refunded > 0:
        manager.append("order already has an earlier refund")
    if requested_amount > remaining:
        manager.append(f"requested {requested_amount} is above the refundable balance {remaining}; reduced")
    if manager:
        return _decide("manager", allowed, why + manager, refs)

    # --- store-wide daily cap for automatic refunds -------------------------------------------
    if refunded_today_pkr + allowed > cfg.auto_refunds_per_day_pkr:
        cap_reason = f"automatic refunds today would exceed {cfg.auto_refunds_per_day_pkr}"
        return _decide("manager", allowed, why + [cap_reason], refs)

    return _decide("auto", allowed, why + ["within automatic policy"], refs)
