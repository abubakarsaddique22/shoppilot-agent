"""Report tool (Step P): the numbers of one day. Aggregates only, no customer rows.

No `from __future__ import annotations` in the tool files: @tool reads the real type hints.
"""
from datetime import date
from typing import Any

from langchain_core.tools import tool

from shoppilot.core.errors import ValidationFailed
from shoppilot.tools.context import get_ctx, tool_guard


@tool
@tool_guard("read")
def sales_summary(day: str | None = None) -> dict[str, Any]:
    """Get the numbers of one day: orders, sales, refunds, late orders and low-stock products. day looks like 2026-10-06; without it, today."""
    ctx = get_ctx()
    try:
        chosen = date.fromisoformat(day) if day else ctx.now().date()
    except ValueError:
        raise ValidationFailed("day must look like 2026-10-06") from None
    return {"ok": True, **ctx.shop.sales_summary(chosen).model_dump(mode="json")}
