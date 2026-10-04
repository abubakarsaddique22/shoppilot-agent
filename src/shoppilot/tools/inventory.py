"""Inventory tools (Step I): read stock, and write a purchase order DRAFT.

The draft goes to the approvals inbox for a human. The agent never emails a supplier.
sales_summary is not here: it needs a new ShopBackend method, so it is built with the Reports agent (Step P).
"""
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from shoppilot.tools.context import audit, find_action, get_ctx, record_action, tool_guard

MAX_PO_QTY = 1000  # quantity cap for one draft


class PurchaseOrderArgs(BaseModel):
    sku: str = Field(min_length=1, max_length=60)
    qty: int = Field(gt=0, le=MAX_PO_QTY)
    supplier: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=3, max_length=120)  # e.g. "po:SKU-123:2026-10-05"


@tool
@tool_guard("read")
def get_inventory(sku: str) -> dict[str, Any]:
    """Get stock on hand, reorder point, average daily sales and the days of stock left for one SKU."""
    level = get_ctx().shop.get_inventory(sku)
    days_left = round(level.on_hand / level.avg_daily_sales, 1) if level.avg_daily_sales > 0 else None
    return {
        "ok": True,
        "sku": level.sku,
        "on_hand": level.on_hand,
        "reorder_point": level.reorder_point,
        "avg_daily_sales": level.avg_daily_sales,
        "days_of_stock": days_left,
    }


@tool("create_purchase_order_draft", args_schema=PurchaseOrderArgs)
@tool_guard("write")
def create_purchase_order_draft(sku: str, qty: int, supplier: str, idempotency_key: str) -> dict[str, Any]:
    """Write a purchase order DRAFT for a low-stock SKU. A human must review and send it. Retrying with the same idempotency_key creates no second draft."""
    earlier = find_action(idempotency_key)
    if earlier is not None:
        return earlier

    draft = get_ctx().shop.create_purchase_order_draft(sku, qty, supplier)
    result = {
        "ok": True,
        "draft_id": draft.id,
        "sku": draft.sku,
        "qty": draft.qty,
        "supplier": draft.supplier,
        "status": draft.status,
    }
    record_action("create_purchase_order_draft", {"sku": sku, "qty": qty, "supplier": supplier}, result, idempotency_key)
    audit("purchase_order_drafted", sku=sku, qty=qty, supplier=supplier)
    return result
