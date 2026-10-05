"""The memory of one support ticket while the graph runs (Step K).

Every node of the support graph reads this state and returns only the fields it changes. LangGraph merges the
result in. The Postgres checkpointer (Step N) saves it after every node, so a crash or a slow human is safe.

What goes in the state: small, plain data (dicts, strings, numbers) that later nodes need.
What does NOT go in the state:
- who is asking (user id, role) and the shop connection: they live in `RunContext` (tools/context.py), which the
  API fills from the signed JWT, never from the model
- the whole order: only the compact summary the tools return
- secrets
"""
from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages

Intent = Literal["order_status", "refund", "exchange", "product_question", "other"]


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
