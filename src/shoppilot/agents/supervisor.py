"""Supervisor (Step O): sorts each incoming item to the right agent and runs that agent's graph.

    classify -> (run_support | run_inventory | run_listing | run_reports | unclear) -> END

An item is a customer email, a command typed in the UI, or a scheduled event. classify makes ONE small
structured-output model call (RouteChoice) and code checks the answer. The supervisor calls no business tools itself.

- Who may reach which agent depends on where the item came from (ALLOWED_ROUTES). A customer email is untrusted text:
  it can only ever reach the support agent, whatever the text says. When only one route is allowed, there is no
  model call at all.
- A scheduled event with a known name is routed by a plain table (EVENT_ROUTES), no model call.
- The model proposes, the code decides: an answer outside the allowed routes, or a model failure, never guesses an
  agent. A staff item becomes "unclear" (nothing runs, the person is told to rephrase). A customer email goes to
  support, which sends anything it cannot handle to a human.
- Each agent runs with its own tool allow-list: RunContext.agent is set while its graph runs (tools/context.py
  AGENT_TOOLS), so a tool outside the list is refused by the tool layer even if the model asks for it.
- The item text is untrusted. It sits inside <item> tags and the prompt says it is data.
- route_item() is a plain function without graph or context. The router evaluation (20 mixed inputs) calls it.
"""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from shoppilot.agents.inventory import build_inventory_graph
from shoppilot.agents.listing import build_listing_graph
from shoppilot.agents.state import Route, SupervisorState
from shoppilot.agents.support import build_support_graph
from shoppilot.core.llm import get_llm
from shoppilot.core.logging import get_logger
from shoppilot.core.prompts import load_prompt
from shoppilot.tools.context import audit, ctx_var, get_ctx

log = get_logger(__name__)

MAX_TEXT_CHARS = 2000  # the classifier does not need more than this to decide

# Which agents an item may reach, by where it came from. A customer can only ever reach support.
ALLOWED_ROUTES: dict[str, frozenset[Route]] = {
    "customer_email": frozenset({"support"}),
    "ui_command": frozenset({"support", "inventory", "listing", "reports"}),
    "scheduled_event": frozenset({"inventory", "reports"}),
}
# Scheduled events carry a fixed name. A known name needs no model.
EVENT_ROUTES: dict[str, Route] = {"daily_report": "reports", "low_stock": "inventory"}
# When the router cannot decide: support (it escalates what it cannot handle) for customers, otherwise nothing runs.
FALLBACK_ROUTE: dict[str, Route] = {"customer_email": "support"}


class RouteChoice(BaseModel):
    """What the model must return. A fixed schema, so it cannot answer in free text."""

    route: Route
    reason: str = Field(default="", max_length=200)


class Routing(TypedDict, total=False):
    route: Route
    reason: str
    by: str
    error: str


# ------------------------------ helpers (not graph nodes)
def _ask_model(text: str) -> RouteChoice:
    safe = text[:MAX_TEXT_CHARS].replace("</item>", "")  # the text cannot close its own data tag
    raw = (
        get_llm(temperature=0)
        .with_structured_output(RouteChoice)
        .invoke([SystemMessage(load_prompt("supervisor")["system"]), HumanMessage(f"<item>\n{safe}\n</item>")])
    )
    return RouteChoice.model_validate(raw)  # the answer may come back as a dict or as the model itself


def _fallback(source: str, reason: str, error: str | None = None) -> Routing:
    routing: Routing = {"route": FALLBACK_ROUTE.get(source, "unclear"), "reason": reason, "by": "fallback"}
    if error:
        routing["error"] = error
    return routing


def route_item(source: str, text: str, event: str | None = None) -> Routing:
    """Decide which agent handles an item. No graph, no run context: safe to call from the evaluation.

    1. unknown source                                -> unclear (never trusted)
    2. scheduled event with a known name             -> the table, no model
    3. only one agent is allowed for this source     -> that agent, no model (customer email: support)
    4. empty text                                    -> unclear
    5. otherwise one model call, checked by code; a failure or a route that is not allowed -> fallback
    """
    allowed = ALLOWED_ROUTES.get(source)
    if allowed is None:
        return {"route": "unclear", "reason": "unknown source", "by": "fallback", "error": "UNKNOWN_SOURCE"}

    if source == "scheduled_event" and event in EVENT_ROUTES and EVENT_ROUTES[event] in allowed:
        return {"route": EVENT_ROUTES[event], "reason": f"scheduled event {event}", "by": "rule"}
    if len(allowed) == 1:
        return {"route": next(iter(allowed)), "reason": f"only this agent may handle a {source}", "by": "rule"}
    if not text.strip():
        return _fallback(source, "the item has no text")

    try:
        choice = _ask_model(text)
    except Exception:  # malformed answer, rate limit, outage
        log.warning("supervisor: no usable model answer, not guessing an agent", exc_info=True)
        return _fallback(source, "the router model gave no usable answer", "ROUTER_FAILED")
    if choice.route != "unclear" and choice.route not in allowed:
        log.warning("supervisor: the model chose a route that is not allowed for %s", source)
        return _fallback(source, "the chosen agent is not allowed for this source", "ROUTE_NOT_ALLOWED")
    return {"route": choice.route, "reason": " ".join(choice.reason.split())[:200], "by": "model"}


@contextmanager
def acting_as(agent: str) -> Iterator[None]:
    """Set the agent name in the run context, so the tool layer applies that agent's allow-list. Always restored,
    also when the graph stops at an interrupt() (the support agent waiting for a human)."""
    ctx = get_ctx()
    previous = ctx.agent
    ctx.agent = agent
    try:
        yield
    finally:
        ctx.agent = previous


def _audit_route(state: SupervisorState, routing: Routing) -> None:
    """One audit row per routing decision. The item text is NOT written (it may hold personal data)."""
    if ctx_var.get(None) is None:  # evaluation or script without a run context
        return
    try:
        audit("route_decided", source=state.get("source"), route=routing["route"], by=routing["by"])
    except Exception:
        log.warning("supervisor: the routing decision could not be written to the audit log", exc_info=True)


def _support_outcome(out: dict[str, Any]) -> str:
    if out.get("escalated"):
        return "Support: the request needs a person. It was escalated and the customer was told someone will reply."
    outgoing = out.get("outgoing") or {}
    if outgoing.get("sent"):
        return f"Support: a reply was queued ({outgoing.get('template')})."
    return "Support: the run ended without a reply being queued. Please check the ticket."


# --------------------------------------------------- nodes
def classify(state: SupervisorState) -> dict[str, Any]:
    routing = route_item(state.get("source", "ui_command"), state.get("text", ""), state.get("event"))
    _audit_route(state, routing)
    update: dict[str, Any] = {
        "route": routing["route"],
        "route_reason": routing.get("reason", ""),
        "route_by": routing.get("by", ""),
    }
    if routing.get("error"):
        update["errors"] = [*state.get("errors", []), routing["error"]]
    return update


def unclear(state: SupervisorState) -> dict[str, Any]:
    """Nothing runs. The sentence tells the staff member what to write."""
    if state.get("source") == "scheduled_event":
        outcome = "The scheduled event is not known to the router. Nothing was run."
    else:
        outcome = (
            "I could not tell which team should handle this, so nothing was done. Please say what you need: "
            "a customer question (with the order number), a stock reorder (with the SKU), "
            "a new product listing (with the product facts), or a sales report."
        )
    return {"outcome": outcome}


def run_reports(state: SupervisorState) -> dict[str, Any]:
    """The Reports agent is built in Step P. Until then the router says so honestly and does nothing."""
    return {
        "outcome": "The Reports agent is not built yet (Step P). Nothing was run.",
        "errors": [*state.get("errors", []), "REPORTS_NOT_BUILT"],
    }


def after_classify(state: SupervisorState) -> Literal["run_support", "run_inventory", "run_listing", "run_reports", "unclear"]:
    route = state.get("route")
    if route == "support":
        return "run_support"
    if route == "inventory":
        return "run_inventory"
    if route == "listing":
        return "run_listing"
    if route == "reports":
        return "run_reports"
    return "unclear"  # unclear, or nothing usable


# ----------------------------------------------------------------------------------------------------------- graph
def build_supervisor_graph(checkpointer: Any = None) -> Any:
    """Wire the router in front of the agent graphs and compile it.

    The agent graphs are compiled without their own checkpointer, so they use the one of the supervisor. That is what
    lets the support agent stop at interrupt() (a manager approval) and resume later: use thread_id = ticket id.
    """
    support = build_support_graph()
    inventory = build_inventory_graph()
    listing = build_listing_graph()

    def ticket_of(state: SupervisorState) -> str:
        return state.get("ticket_id") or get_ctx().ticket_id

    def run_support(state: SupervisorState, config: RunnableConfig) -> dict[str, Any]:
        with acting_as("support"):
            out = support.invoke({"ticket_id": ticket_of(state), "messages": [HumanMessage(state.get("text", ""))]}, config)
        return {"outcome": _support_outcome(out), "errors": [*state.get("errors", []), *out.get("errors", [])]}

    def run_inventory(state: SupervisorState, config: RunnableConfig) -> dict[str, Any]:
        with acting_as("inventory"):
            out = inventory.invoke(
                {"ticket_id": ticket_of(state), "request": state.get("text", ""), "sku": state.get("sku")}, config
            )
        return {
            "outcome": out.get("outcome", "Inventory: nothing to do."),
            "errors": [*state.get("errors", []), *out.get("errors", [])],
        }

    def run_listing(state: SupervisorState, config: RunnableConfig) -> dict[str, Any]:
        with acting_as("listing"):
            out = listing.invoke({"ticket_id": ticket_of(state), "request": state.get("text", "")}, config)
        return {
            "outcome": out.get("outcome", "Listing: nothing to do."),
            "errors": [*state.get("errors", []), *out.get("errors", [])],
        }

    g = StateGraph(SupervisorState)
    nodes: list[tuple[str, Callable[..., dict[str, Any]]]] = [
        ("classify", classify),
        ("run_support", run_support),
        ("run_inventory", run_inventory),
        ("run_listing", run_listing),
        ("run_reports", run_reports),
        ("unclear", unclear),
    ]
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify", after_classify, ["run_support", "run_inventory", "run_listing", "run_reports", "unclear"]
    )
    for name in ["run_support", "run_inventory", "run_listing", "run_reports", "unclear"]:
        g.add_edge(name, END)
    return g.compile(checkpointer=checkpointer)
