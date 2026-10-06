"""The state of every graph (Steps K, L, O). All state classes live here.

Each LangGraph graph has its own state: every node reads it and returns only the fields it changes, and LangGraph
merges the result in. The Postgres checkpointer saves it after every node.

    TicketState      support graph
    InventoryState   inventory graph
    ListingState     listing graph
    SupervisorState  the router in front of the three graphs

What goes in a state: small, plain data (dicts, strings, numbers) that later nodes need.
What does NOT go in a state:
- who is asking (user id, role) and the shop connection: they live in `RunContext` (tools/context.py), which the
  API fills from the signed JWT, never from the model
- the whole order: only the compact summary the tools return
- secrets
"""
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

Intent = Literal["order_status", "refund", "exchange", "product_question", "other"]
Source = Literal["customer_email", "ui_command", "scheduled_event"]
Route = Literal["support", "inventory", "listing", "reports", "unclear"]


class TicketState(TypedDict, total=False):
    # conversation with the customer and the internal tool messages (add_messages appends, never replaces)
    messages: Annotated[list, add_messages]
    ticket_id: str  # also the LangGraph thread_id, so the checkpointer finds the run again

    intent: Intent  # set by triage
    order_ref: str | None  # the order number the customer wrote, or None (set by triage)
    order: dict  # compact order facts from get_order (set by gather_facts)
    facts: list[dict]  # tool results the decision is based on, each with a source id (evidence)

    proposal: dict  # what the model proposes: action, amount, reason, evidence ids (set by decide)
    ruling: dict  # what the policy engine decides: tier, allowed amount, reasons (set by rules). It wins.
    approval: dict  # the human decision: status, by, note (set by approval_gate after interrupt)

    actions_taken: list[str]  # idempotency keys of the writes already done in this run
    result: dict  # what execute did: {"refund_id", "order_id", "amount_pkr", "status"} (set by execute)
    verified: bool  # set by verify: the refund really shows on the order
    escalated: bool  # set by escalate: a human task exists, so reply must not promise anything else
    outgoing: dict  # set by reply: {"template", "sent", "error"?}. Not named "reply": a state key cannot share a node's name
    budget: dict  # {"retries_used": 0}; tool calls are counted in RunContext
    errors: list[str]  # structured errors for verify and escalate to read


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


class ListingState(TypedDict, total=False):
    ticket_id: str
    request: str  # the product facts written by the staff member
    idempotency_key: str
    draft_fields: dict[str, Any]  # what the model wrote and the code accepted
    draft: dict[str, Any]  # what the tool saved
    outcome: str
    errors: list[str]


class SupervisorState(TypedDict, total=False):
    ticket_id: str  # the run id (also the LangGraph thread_id)
    source: Source  # where the item came from
    text: str  # the item text (untrusted)
    sku: str | None  # optional, given by the UI or the scheduler; passed to the inventory agent
    event: str | None  # name of a scheduled event, e.g. "low_stock"
    route: Route  # set by classify
    route_reason: str  # one line, for the audit trail and the UI
    route_by: str  # "rule" (code decided), "model", or "fallback" (the model failed or was not allowed)
    outcome: str  # the sentence shown to the staff member
    errors: list[str]
