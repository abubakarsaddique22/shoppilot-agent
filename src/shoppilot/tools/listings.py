"""Listing tools (Step I): read a product, and write a product DRAFT.

The agent never publishes: the backend always saves the product with status "draft".
The text is built into simple HTML and escaped, so nothing the model (or a customer) wrote can inject markup into the store admin.
"""
from html import escape
from typing import Annotated, Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from shoppilot.tools.context import audit, find_action, get_ctx, record_action, tool_guard

Short = Annotated[str, Field(max_length=200)]
Tag = Annotated[str, Field(max_length=40)]


class ProductDraftArgs(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    bullet_points: list[Short] = Field(default_factory=list, max_length=8)
    tags: list[Tag] = Field(default_factory=list, max_length=10)
    product_type: str = Field(default="", max_length=60)
    vendor: str = Field(default="", max_length=60)
    idempotency_key: str = Field(min_length=3, max_length=120)  # e.g. "listing:SKU-123:v1"


@tool
@tool_guard("read")
def get_product(sku: str) -> dict[str, Any]:
    """Get title, category, price, supplier and whether the product is refundable for one SKU."""
    product = get_ctx().shop.get_product(sku)
    return {
        "ok": True,
        "sku": product.sku,
        "title": product.title,
        "category": product.category,
        "price_pkr": product.price_pkr,
        "refundable": product.refundable,
        "supplier": product.supplier,
    }


def _body_html(description: str, bullet_points: list[str]) -> str:
    html = f"<p>{escape(description)}</p>"
    if bullet_points:
        html += "<ul>" + "".join(f"<li>{escape(b)}</li>" for b in bullet_points) + "</ul>"
    return html


@tool("create_product_draft", args_schema=ProductDraftArgs)
@tool_guard("write")
def create_product_draft(
    title: str,
    description: str,
    idempotency_key: str,
    bullet_points: list[str] | None = None,
    tags: list[str] | None = None,
    product_type: str = "",
    vendor: str = "",
) -> dict[str, Any]:
    """Save a new product listing as a DRAFT (title, description, bullet points, tags). It is never published by the agent. Retrying with the same idempotency_key creates no second draft."""
    earlier = find_action(idempotency_key)
    if earlier is not None:
        return earlier

    fields = {
        "title": title,
        "body_html": _body_html(description, bullet_points or []),
        "tags": tags or [],
        "product_type": product_type,
        "vendor": vendor,
    }
    draft = get_ctx().shop.create_product_draft(fields)
    result = {"ok": True, "draft_id": draft.id, "title": draft.title, "status": draft.status}
    record_action("create_product_draft", {"title": title}, result, idempotency_key)
    audit("product_drafted", title=title, draft_id=draft.id)
    return result
