"""Listing agent (Step O): writes a product listing DRAFT from the facts a staff member gives.

    write_listing -> save_draft -> report          (write_listing failing goes straight to report)

- Level L1: the draft is saved with status "draft". The agent can never publish: the backend does not allow it.
- The model writes the text, the code checks it before anything is saved: field lengths (the same schema the tool
  uses), and no email address, link or HTML in any field. A rejected answer gets ONE retry; then no draft is saved.
- The facts are untrusted text (they may be pasted from a supplier page). They sit inside <product_facts> tags and the
  prompt says they are data. The worst a hostile text can do is to produce a draft that a person reads.
- Tool allow-list: get_product, create_product_draft (tools/context.py AGENT_TOOLS).
- The same facts twice give the same draft (the idempotency key is a hash of the facts).
"""
import hashlib
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from shoppilot.agents.state import ListingState
from shoppilot.core.llm import get_llm
from shoppilot.core.logging import get_logger
from shoppilot.core.prompts import load_prompt
from shoppilot.tools.listings import ProductDraftArgs, create_product_draft

log = get_logger(__name__)

MAX_ATTEMPTS = 2  # the first answer plus one retry
MAX_REQUEST_CHARS = 3000
UNSAFE_TEXT = re.compile(r"@|https?:|www\.|<\s*[a-z/!]", re.IGNORECASE)  # email address, link, HTML tag


class ListingText(BaseModel):
    """The answer the model must give. The limits match the tool schema (ProductDraftArgs)."""

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    bullet_points: list[str] = Field(default_factory=list, max_length=8)
    tags: list[str] = Field(default_factory=list, max_length=10)
    product_type: str = Field(default="", max_length=60)
    vendor: str = Field(default="", max_length=60)


# ------------------------------------------------------------------------------------------------ helpers (not graph nodes)
def listing_key(request: str) -> str:
    return "listing:" + hashlib.sha1(request.strip().encode()).hexdigest()[:12] + ":v1"


def _texts(fields: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for value in fields.values():
        values.extend(value if isinstance(value, list) else [value])
    return [str(v) for v in values]


def check_fields(fields: dict[str, Any], key: str) -> str | None:
    """Check what the model wrote. Returns a problem for the model (field names only, never its own words) or None."""
    try:
        ProductDraftArgs(**fields, idempotency_key=key)
    except ValidationError as err:
        names = sorted({str(e["loc"][0]) for e in err.errors() if e["loc"]})
        return "these fields are not valid: " + ", ".join(names)
    if any(UNSAFE_TEXT.search(text) for text in _texts(fields)):
        return "no email address, link or HTML tag is allowed in any field"
    return None


# ----------------------------------------------------------------------------------------------------------- nodes
def write_listing(state: ListingState) -> dict[str, Any]:
    request = state.get("request", "").strip()
    if not request:
        return {"errors": ["NO_REQUEST"]}
    safe = request[:MAX_REQUEST_CHARS].replace("</product_facts>", "")  # the text cannot close its own data tag
    key = listing_key(request)
    messages: list[Any] = [
        SystemMessage(load_prompt("listing")["system"]),
        HumanMessage(f"<product_facts>\n{safe}\n</product_facts>"),
    ]
    for attempt in range(MAX_ATTEMPTS):
        fields: dict[str, Any] = {}
        try:
            raw = get_llm(temperature=0.4).with_structured_output(ListingText).invoke(messages)
            fields = ListingText.model_validate(raw).model_dump()
            problem = check_fields(fields, key)
        except Exception:
            log.warning("listing: the model answer was missing or malformed", exc_info=True)
            problem = "your answer did not match the required fields"
        if problem is None:
            return {"draft_fields": fields, "idempotency_key": key}
        if attempt + 1 < MAX_ATTEMPTS:
            messages = [*messages, HumanMessage(f"Your answer was rejected: {problem}. Answer again.")]
    return {"errors": ["LISTING_FAILED"]}


def save_draft(state: ListingState) -> dict[str, Any]:
    result = create_product_draft.invoke({**state["draft_fields"], "idempotency_key": state["idempotency_key"]})
    if not result.get("ok"):
        return {"errors": [str(result.get("error", "TOOL_ERROR"))]}
    return {"draft": {k: v for k, v in result.items() if k != "ok"}}


def report(state: ListingState) -> dict[str, Any]:
    """The sentence for the staff member, built by code."""
    errors = state.get("errors", [])
    draft = state.get("draft")
    if "NO_REQUEST" in errors:
        outcome = "Please write the product facts: name, material, size, colour and anything a buyer should know."
    elif errors:
        outcome = f"No listing draft was saved: {', '.join(errors)}. Please check the facts and try again."
    elif draft:
        outcome = (
            f"Product draft #{draft['draft_id']} \"{draft['title']}\" is saved. It is NOT published: "
            "a person must review it in the store admin and publish it."
        )
    else:
        outcome = "Nothing to do."
    return {"outcome": outcome}


# ----------------------------------------------------------------------------------------------------------- graph
def after_write(state: ListingState) -> Literal["save_draft", "report"]:
    return "report" if state.get("errors") else "save_draft"


def build_listing_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(ListingState)
    for name, fn in [("write_listing", write_listing), ("save_draft", save_draft), ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "write_listing")
    g.add_conditional_edges("write_listing", after_write)
    g.add_edge("save_draft", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
