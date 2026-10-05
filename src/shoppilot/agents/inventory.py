"""Inventory agent (Step O): reads stock, proposes a reorder quantity, writes a purchase order DRAFT.

    read_stock -> propose -> write_draft -> summarize          (stops early when there is nothing to order)

- The agent is level L1: it only drafts. The draft goes to the approvals inbox for a person. It never emails a supplier.
- The model proposes, the code decides: the model may adjust the suggested quantity, but only between 1 and twice the
  suggestion (bounded_qty). If the model fails, the plain calculation is used, so a model outage cannot stop the draft.
- Only the nodes propose (model) and the tools below touch anything. Tool allow-list: get_inventory, get_product,
  create_purchase_order_draft (tools/context.py AGENT_TOOLS), enforced by the tool layer.
- Writing the same SKU twice on the same day gives the same draft and the same approval (idempotency key).
"""
import json
import math
import re
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from shoppilot.agents.support import load_prompt
from shoppilot.approvals.service import request_approval
from shoppilot.core.config import settings
from shoppilot.core.llm import get_llm
from shoppilot.core.logging import get_logger
from shoppilot.tools.context import audit, get_ctx
from shoppilot.tools.inventory import MAX_PO_QTY, create_purchase_order_draft, get_inventory
from shoppilot.tools.listings import get_product

log = get_logger(__name__)

COVER_DAYS = 30  # one purchase order should cover about 30 days of sales
MAX_QTY_FACTOR = 2  # the model may go up to twice the suggested quantity, never more
SKU_PATTERN = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)+", re.IGNORECASE)  # KURTA-M-BLK, EARBUDS-TWS-01


class InventoryState(TypedDict, total=False):
    ticket_id: str  # the run id: actions and approvals point to a row with this id
    request: str  # what the staff member typed (untrusted text, only used to find the SKU)
    sku: str | None  # set by the caller (scheduler) or found in the request
    stock: dict[str, Any]
    product: dict[str, Any]
    proposal: dict[str, Any]
    draft: dict[str, Any]
    approval_id: int
    outcome: str  # the sentence shown to the staff member
    errors: list[str]


class QtyProposal(BaseModel):
    qty: int = Field(gt=0, le=MAX_PO_QTY)
    reason: str = Field(default="", max_length=300)


# ------------------------------------------------------------------------------------------------ plain functions
def find_sku(text: str) -> str | None:
    match = SKU_PATTERN.search(text)
    return match.group(0).upper() if match else None


def suggested_qty(stock: dict[str, Any]) -> int:
    """Units that cover COVER_DAYS of sales on top of what is on the shelf. No sales history: twice the reorder point."""
    velocity = float(stock["avg_daily_sales"])
    target = math.ceil(velocity * COVER_DAYS) if velocity > 0 else int(stock["reorder_point"]) * 2
    return max(1, min(MAX_PO_QTY, target - int(stock["on_hand"])))


def bounded_qty(qty: int, suggested: int) -> int:
    """The model may adjust the suggestion, but only within 1 and twice the suggestion. Anything else: the suggestion."""
    return qty if 1 <= qty <= min(MAX_PO_QTY, suggested * MAX_QTY_FACTOR) else suggested


def _error_code(result: dict[str, Any]) -> str:
    return str(result.get("error", "TOOL_ERROR"))


def _ask_model(stock: dict[str, Any], suggested: int) -> QtyProposal | None:
    facts = {**stock, "suggested_qty": suggested, "cover_days": COVER_DAYS}
    try:
        raw = (
            get_llm(temperature=0.2)
            .with_structured_output(QtyProposal)
            .invoke(
                [
                    SystemMessage(load_prompt("inventory")["system"]),
                    HumanMessage(f"<facts>\n{json.dumps(facts)}\n</facts>"),
                ]
            )
        )
        return QtyProposal.model_validate(raw)
    except Exception:
        log.warning("inventory: no usable model answer, using the suggested quantity", exc_info=True)
        return None


# ----------------------------------------------------------------------------------------------------------- nodes
def read_stock(state: InventoryState) -> dict[str, Any]:
    sku = (state.get("sku") or "").strip().upper() or find_sku(state.get("request", ""))
    if not sku:
        return {"errors": ["NO_SKU"]}
    stock = get_inventory.invoke({"sku": sku})
    if not stock.get("ok"):
        return {"sku": sku, "errors": [_error_code(stock)]}
    product = get_product.invoke({"sku": sku})
    if not product.get("ok"):
        return {"sku": sku, "errors": [_error_code(product)]}
    return {
        "sku": sku,
        "stock": {k: v for k, v in stock.items() if k != "ok"},
        "product": {k: v for k, v in product.items() if k != "ok"},
    }


def propose(state: InventoryState) -> dict[str, Any]:
    stock = state["stock"]
    if int(stock["on_hand"]) > int(stock["reorder_point"]):
        return {"proposal": {"action": "no_action", "reason": "stock is above the reorder point"}}

    suggested = suggested_qty(stock)
    answer = _ask_model(stock, suggested)
    qty = bounded_qty(answer.qty, suggested) if answer else suggested
    reason = (answer.reason.strip().replace("\n", " ") if answer else "") or (
        f"{stock['on_hand']} on hand is at or below the reorder point {stock['reorder_point']}; "
        f"about {COVER_DAYS} days of sales"
    )
    return {
        "proposal": {
            "action": "reorder",
            "qty": qty,
            "reason": reason,
            "suggested_qty": suggested,
            "from_model": answer is not None,
        }
    }


def write_draft(state: InventoryState) -> dict[str, Any]:
    ctx = get_ctx()
    sku = str(state["sku"])
    proposal = state["proposal"]
    supplier = str((state.get("product") or {}).get("supplier") or "").strip()
    if not supplier:
        return {"errors": ["NO_SUPPLIER"]}

    key = f"po:{sku}:{ctx.now().date().isoformat()}"  # same SKU, same day: same draft
    result = create_purchase_order_draft.invoke(
        {"sku": sku, "qty": int(proposal["qty"]), "supplier": supplier, "idempotency_key": key}
    )
    if not result.get("ok"):
        return {"errors": [_error_code(result)]}
    draft = {k: v for k, v in result.items() if k != "ok"}

    approval_id, created = request_approval(
        ctx.session_factory,
        ticket_id=state.get("ticket_id") or ctx.ticket_id,
        key=key,
        tier="manager",
        payload={"type": "purchase_order", **draft, "reason": proposal["reason"]},
        ttl_hours=settings.approval_ttl_hours,
        now=ctx.now(),
        action="purchase_order",
    )
    if created:
        audit("approval_requested", approval_id=approval_id, action="purchase_order", tier="manager")
    return {"draft": draft, "approval_id": approval_id}


def summarize(state: InventoryState) -> dict[str, Any]:
    """The sentence for the staff member. Built by code, so it can never promise something that did not happen."""
    errors = state.get("errors", [])
    sku = state.get("sku") or "that product"
    proposal = state.get("proposal") or {}
    draft = state.get("draft")
    if "NO_SKU" in errors:
        outcome = "I could not find a SKU in the request. Write it like this: EARBUDS-TWS-01."
    elif errors:
        outcome = f"No purchase order was prepared for {sku}: {', '.join(errors)}."
    elif proposal.get("action") == "no_action":
        stock = state["stock"]
        outcome = (
            f"{sku}: {stock['on_hand']} on hand, reorder point {stock['reorder_point']}. "
            "Stock is fine, no purchase order needed."
        )
    elif draft:
        outcome = (
            f"Purchase order draft #{draft['draft_id']}: {draft['qty']} x {draft['sku']} from {draft['supplier']}. "
            "It is waiting for review in the approvals inbox. Nothing was sent to the supplier."
        )
    else:
        outcome = "Nothing to do."
    return {"outcome": outcome}


# ----------------------------------------------------------------------------------------------------------- graph
def after_read(state: InventoryState) -> Literal["propose", "summarize"]:
    return "summarize" if state.get("errors") else "propose"


def after_propose(state: InventoryState) -> Literal["write_draft", "summarize"]:
    return "write_draft" if (state.get("proposal") or {}).get("action") == "reorder" else "summarize"


def build_inventory_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(InventoryState)
    for name, fn in [
        ("read_stock", read_stock),
        ("propose", propose),
        ("write_draft", write_draft),
        ("summarize", summarize),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "read_stock")
    g.add_conditional_edges("read_stock", after_read)
    g.add_conditional_edges("propose", after_propose)
    g.add_edge("write_draft", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)
