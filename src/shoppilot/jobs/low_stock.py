"""Low-stock alert job (Step P): finds products at or below their reorder point and asks the Inventory agent for a draft.

    uv run python -m shoppilot.jobs.low_stock        # cron runs this every few hours (see docs/runbook.md)

For each low-stock SKU the Inventory agent writes a purchase order DRAFT and an approval for a manager. It never emails
a supplier. The job is safe to run again and again:
- a SKU that already has a pending purchase-order approval is skipped (no pile of drafts while a person has not looked)
- the same SKU on the same day gives the same draft anyway (idempotency key inside the Inventory agent)
- one SKU failing never stops the others

The drafts hang on one ticket per day, "low-stock-YYYY-MM-DD", because actions and approvals point to a ticket row.
"""
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.agents.inventory import build_inventory_graph
from shoppilot.core.config import settings
from shoppilot.core.logging import get_logger
from shoppilot.db.models import ApprovalRow, TicketRow, utcnow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.mockshop import MockShop
from shoppilot.tools.context import RunContext, audit, ctx_var

log = get_logger(__name__)


# ------------------------------------------------------------------------------------------------ helpers
def pending_skus(sf: sessionmaker[Session]) -> set[str]:
    """SKUs that already wait for a person to review a purchase order."""
    with sf() as session:
        rows = session.scalars(
            select(ApprovalRow).where(ApprovalRow.action == "purchase_order", ApprovalRow.status == "pending")
        ).all()
    return {str(row.payload_json.get("sku")) for row in rows}


def ensure_ticket(sf: sessionmaker[Session], ticket_id: str, email: str) -> None:
    with sf() as session:
        if session.get(TicketRow, ticket_id) is None:
            session.add(
                TicketRow(id=ticket_id, channel="scheduler", customer_email=email, subject="Low stock check", status="done")
            )
            session.commit()


# ------------------------------------------------------------------------------------------------ the job
def run_low_stock(
    sf: sessionmaker[Session] | None = None,
    shop: MockShop | None = None,
    now: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Check the stock once. Returns {"checked": n, "drafted": [sku], "skipped": [sku], "failed": {sku: reason}}."""
    sf = sf or make_session_factory(make_engine())
    shop = shop or MockShop(sf, now=now)
    day = now().date()
    ticket_id = f"low-stock-{day.isoformat()}"
    email = settings.owner_email or "system@example.com"

    low = shop.sales_summary(day).low_stock  # the job is trusted code: it asks the shop directly
    result: dict[str, Any] = {"checked": len(low), "drafted": [], "skipped": [], "failed": {}}
    if not low:
        return result

    waiting = pending_skus(sf)
    ensure_ticket(sf, ticket_id, email)
    graph = build_inventory_graph()
    for item in low:
        if item.sku in waiting:
            result["skipped"].append(item.sku)
            continue
        # a fresh context for every SKU, so each one has its own tool budget
        ctx = RunContext(
            shop=shop, session_factory=sf, ticket_id=ticket_id, customer_email=email,
            actor_id="system", actor_role="system", now=now, agent="inventory",
        )  # fmt: skip
        token = ctx_var.set(ctx)
        try:
            config = {
                "metadata": {
                    "ticket_id": ticket_id, "sku": item.sku, "role": "system", "env": settings.env,
                    "model": f"{settings.llm_provider}/{settings.llm_model}", "job": "low_stock",
                },
                "tags": [settings.env, "scheduled"],
            }
            out = graph.invoke({"ticket_id": ticket_id, "request": "", "sku": item.sku}, config)
            if out.get("draft"):
                result["drafted"].append(item.sku)
                audit("low_stock_drafted", sku=item.sku)
            else:
                result["failed"][item.sku] = ", ".join(out.get("errors", [])) or "no draft was made"
        except Exception as err:  # one bad SKU must not stop the others
            log.exception("low stock: the inventory agent failed for %s", item.sku)
            result["failed"][item.sku] = type(err).__name__
        finally:
            ctx_var.reset(token)
    return result


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()  # the LangSmith SDK reads LANGSMITH_* from the environment
    print(run_low_stock())
