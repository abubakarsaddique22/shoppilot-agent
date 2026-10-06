"""Read tools for orders, shipments and policies (Step I).

Every tool here only shows data of the customer who wrote the ticket (ctx.customer_email). An order of someone
else gives the same ORDER_NOT_FOUND as an order that does not exist, so nothing leaks.
The model gets a small summary, never the whole order: `note` (untrusted customer text) and the internal flags
(flagged customer, refund history) stay inside the policy engine.

No `from __future__ import annotations` in the tool files: @tool reads the real type hints.
"""
from typing import Any

from langchain_core.tools import tool

from shoppilot.core.errors import NotFound, OrderNotFound, ShopBackendError
from shoppilot.guardrails.validators import check_order_ref
from shoppilot.kb.retriever import search_policy as kb_search
from shoppilot.shop.base import Order
from shoppilot.tools.context import get_ctx, tool_guard


def load_own_order(order_ref: str) -> Order:
    """The order, but only if it belongs to the customer of this ticket. Shared with the refund tools.
    The reference must look like an order number (not like a sentence) before the shop is asked."""
    ctx = get_ctx()
    order = ctx.shop.get_order(check_order_ref(order_ref))
    if order.customer_email.strip().lower() != ctx.customer_email.strip().lower():
        raise OrderNotFound(f"order {order_ref} not found", details={"order_id": order_ref})
    return order


def order_summary(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "status": order.status,
        "payment_method": order.payment_method,
        "amount_paid": order.amount_paid,
        "refunded_total": order.refunded_total,
        "placed_at": order.placed_at.isoformat(),
        "estimated_delivery": order.estimated_delivery.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "days_late": order.days_late,
        "is_overdue": order.is_overdue,
        "tracking_no": order.tracking_no,
        "items": [{"sku": i.sku, "title": i.title, "qty": i.qty} for i in order.items],
    }


@tool
@tool_guard("read")
def get_order(order_ref: str) -> dict[str, Any]:
    """Get status, dates, amounts and items of one order of THIS customer. order_ref is the number the customer wrote, like 88731 or #88731."""
    return {"ok": True, "order": order_summary(load_own_order(order_ref))}


@tool
@tool_guard("read")
def find_orders_by_email() -> dict[str, Any]:
    """List the latest orders (at most 5) of the customer who wrote this ticket. Use it when the customer gave no order number."""
    ctx = get_ctx()
    orders = ctx.shop.find_orders(ctx.customer_email, limit=5)
    return {
        "ok": True,
        "orders": [
            {"id": o.id, "status": o.status, "amount_paid": o.amount_paid, "placed_at": o.placed_at.isoformat()}
            for o in orders
        ],
    }


@tool
@tool_guard("read")
def track_shipment(tracking_no: str) -> dict[str, Any]:
    """Get the courier status and the last events of a shipment of THIS customer. If the courier does not answer, the status is 'unknown'."""
    ctx = get_ctx()
    try:
        shipment = ctx.shop.track_shipment(tracking_no)
    except ShopBackendError:
        return {"ok": True, "status": "unknown", "events": []}
    try:
        load_own_order(shipment.order_id)
    except OrderNotFound:
        raise NotFound(f"shipment {tracking_no} not found", code="SHIPMENT_NOT_FOUND") from None
    return {
        "ok": True,
        "tracking_no": shipment.tracking_no,
        "courier": shipment.courier,
        "status": shipment.status,
        "last_update": shipment.last_update.isoformat(),
        "events": [
            {"ts": e.ts.isoformat(), "status": e.status, "location": e.location} for e in shipment.events[-5:]
        ],
    }


@tool
@tool_guard("read")
def search_policy(query: str) -> dict[str, Any]:
    """Search the store policies (returns, shipping, exchange). Cite the section of every hit. If ok is false with NO_POLICY_FOUND, do not invent a rule: escalate."""
    ctx = get_ctx()
    with ctx.session_factory() as session:
        result = kb_search(session, query)
    return result.model_dump()
