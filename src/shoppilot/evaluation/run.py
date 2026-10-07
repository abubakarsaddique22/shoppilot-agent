"""Step U: run ONE evaluation case on a fresh, seeded, in-memory store and report what really happened.

Every case starts from the same known state (blueprint section 14): a new SQLite MockShop, seeded with the fixed clock
EVAL_NOW, and a ticket row. The real graph runs with the real model from .env. A human decision is never asked for:
the case lists scripted decisions, and this module writes them through the approvals service (the approvals TABLE is
the source of truth, exactly like in production) and resumes the graph.

What comes back is the OUTCOME (refund in the shop, tier, escalation, emails, approvals, audit) and the TRAJECTORY
(which tools were called), not how nice the text sounds. evaluators.py turns it into scores.

Not faked: the model, the tools, the policy engine, the approvals service, the graph.
Faked: the policy search (it needs pgvector). Use policy_search("real") to use the Postgres knowledge base instead.
One global patch is used for the fault llm_down, so run cases one after the other (max_concurrency 1).
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from time import perf_counter
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import Engine, func, select

from shoppilot.agents import support
from shoppilot.agents.inventory import build_inventory_graph
from shoppilot.agents.listing import build_listing_graph
from shoppilot.agents.supervisor import acting_as
from shoppilot.agents.support import build_support_graph
from shoppilot.approvals.service import record_decision
from shoppilot.db.models import AppBase, ApprovalRow, AuditLogRow, MessageRow, TicketRow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.evaluation.dataset import EVAL_NOW
from shoppilot.kb.retriever import PolicyHit, PolicySearchResult
from shoppilot.shop.mock_models import OrderRow, RefundRow
from shoppilot.shop.mockshop import MockShop
from shoppilot.shop.seed import seed_database
from shoppilot.tools import orders as orders_module
from shoppilot.tools.context import RunContext, ctx_var


# ---------------------------------------------------------------------------------------------------- helpers
def new_env() -> tuple[Engine, Any, MockShop]:
    """A fresh seeded store. Same seed and same clock every time, so the same case sees the same data."""
    engine = make_engine("sqlite://")
    seed_database(engine, EVAL_NOW)
    AppBase.metadata.create_all(engine)
    sf = make_session_factory(engine)
    return engine, sf, MockShop(sf, now=lambda: EVAL_NOW)


def orders_of(sf: Any, shop: MockShop, scenario: str) -> list[Any]:
    with sf() as s:
        names = s.scalars(select(OrderRow.name).where(OrderRow.scenario == scenario).order_by(OrderRow.id)).all()
    return [shop.get_order(n) for n in names]


def fill(text: str, values: dict[str, str]) -> str:
    """Fill {order}, {other} and {other_email}. Plain replace, so any other brace in a hostile text is left alone."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


class Recorder(BaseCallbackHandler):
    """Collects the tool names and the token usage of one case. LangGraph hands the callbacks to the tools."""

    def __init__(self) -> None:
        self.tools: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:  # type: ignore[override]
        name = (serialized or {}).get("name") or kwargs.get("name")
        if name:
            self.tools.append(str(name))

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.llm_calls += 1
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                self.input_tokens += int(usage.get("input_tokens", 0))
                self.output_tokens += int(usage.get("output_tokens", 0))


# --------------------------------------------------------------------------------------- policy search and faults
def _fixed_hits(query: str) -> list[PolicyHit]:
    """Stand-in policy sections. They only state what the settings already say (14 days, 5 days late)."""

    def hit(doc: str, section: str, text: str) -> PolicyHit:
        return PolicyHit(doc=doc, section=section, text=text, version="v1", score=0.9)

    if "exchange" in query:
        return [hit("exchange", "Exchanges > Size and colour", "An unused item can be exchanged within 14 days of delivery.")]
    if "refund" in query:
        return [
            hit("returns", "Returns > Refund window", "A refund can be requested within 14 days of delivery."),
            hit("returns", "Returns > Late deliveries", "A refund is possible when the order is more than 5 days late."),
            hit("returns", "Returns > Damaged items", "A damaged item needs a short description. A manager approves the refund."),
        ]
    return [hit("shipping", "Shipping > Delivery estimate", "An order is late when it arrives after the estimated delivery date.")]


@contextlib.contextmanager
def policy_search(mode: str = "fixed") -> Iterator[None]:
    """mode "fixed": no database needed. mode "real": the Postgres knowledge base from SHOP_DATABASE_URL."""
    original = orders_module.kb_search
    if mode == "real":
        pg_sessions = make_session_factory(make_engine())

        def search(session: Any, query: str) -> Any:
            with pg_sessions() as pg:
                return original(pg, query)

    else:

        def search(session: Any, query: str) -> Any:
            return PolicySearchResult(ok=True, hits=_fixed_hits(query))

    orders_module.kb_search = search  # type: ignore[assignment]
    try:
        yield
    finally:
        orders_module.kb_search = original


@contextlib.contextmanager
def injected_fault(name: str | None, shop: MockShop) -> Iterator[None]:
    """courier_down: track_shipment raises. llm_down: the model call raises. Both must end in a safe answer."""
    if name == "courier_down":
        from shoppilot.core.errors import ShopBackendError

        def broken(tracking_no: str) -> Any:
            raise ShopBackendError("the courier API is down")

        shop.track_shipment = broken  # type: ignore[method-assign]
        try:
            yield
        finally:
            shop.__dict__.pop("track_shipment", None)
    elif name == "llm_down":
        original = support.get_llm

        def down(temperature: float = 0.0) -> Any:
            raise RuntimeError("the model is down")

        support.get_llm = down  # type: ignore[assignment]
        try:
            yield
        finally:
            support.get_llm = original
    else:
        yield


# ------------------------------------------------------------------------------------------------- reading results
def _context(sf: Any, shop: MockShop, ticket_id: str, email: str) -> RunContext:
    with sf() as s:
        s.add(TicketRow(id=ticket_id, customer_email=email))
        s.commit()
    return RunContext(
        shop=shop, session_factory=sf, ticket_id=ticket_id, customer_email=email,
        actor_id="eval", actor_role="system", now=lambda: EVAL_NOW,
    )  # fmt: skip


def _refund_rows(sf: Any) -> int:
    with sf() as s:
        return s.scalar(select(func.count(RefundRow.id))) or 0


def _pending_approval(sf: Any, ticket_id: str) -> int | None:
    with sf() as s:
        return s.scalar(
            select(ApprovalRow.id)
            .where(ApprovalRow.ticket_id == ticket_id, ApprovalRow.status == "pending")
            .order_by(ApprovalRow.id.desc())
        )


def _collect(sf: Any, ticket_id: str) -> dict[str, Any]:
    with sf() as s:
        approvals = [
            {"status": r.status, "tier": r.tier, "action": r.action}
            for r in s.scalars(select(ApprovalRow).where(ApprovalRow.ticket_id == ticket_id).order_by(ApprovalRow.id))
        ]
        emails = list(
            s.scalars(
                select(MessageRow.body)
                .where(MessageRow.ticket_id == ticket_id, MessageRow.direction == "outbound")
                .order_by(MessageRow.id)
            )
        )
        audit_events = [
            row.event for row in s.scalars(select(AuditLogRow)) if (row.detail_json or {}).get("ticket_id") == ticket_id
        ]
    return {"approvals": approvals, "emails": emails, "audit": audit_events}


def _stats(rec: Recorder, ctx: RunContext, seconds: float, error: str | None) -> dict[str, Any]:
    return {
        "tools": rec.tools,
        "tool_calls": ctx.reads_used + ctx.writes_used,
        "seconds": round(seconds, 3),
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "llm_calls": rec.llm_calls,
        "error": error,
    }


# ----------------------------------------------------------------------------------------------------- the agents
def _run_support(case: dict[str, Any], sf: Any, shop: MockShop) -> dict[str, Any]:
    mine = orders_of(sf, shop, case["scenario"])[int(case["index"])]
    other = next(o for o in orders_of(sf, shop, "on_time_status") if o.customer_email != mine.customer_email)
    values = {"order": mine.id, "other": other.id, "other_email": other.customer_email}
    text = fill(case["ticket_text"], values)

    ticket_id = f"T-ev-{case['id']}"
    ctx = _context(sf, shop, ticket_id, mine.customer_email)
    rec = Recorder()
    graph = build_support_graph(InMemorySaver())
    config: dict[str, Any] = {"configurable": {"thread_id": ticket_id}, "callbacks": [rec]}
    rows_before = _refund_rows(sf)
    seconds, error = 0.0, None

    token = ctx_var.set(ctx)
    try:
        with injected_fault(case.get("fault"), shop):
            started = perf_counter()
            graph.invoke(
                {"ticket_id": ticket_id, "messages": [HumanMessage(text)], "actions_taken": [], "errors": []}, config
            )
            seconds += perf_counter() - started
            for decision in case.get("approvals") or []:  # the scripted human decisions
                if tuple(graph.get_state(config).next) != ("approval_gate",):
                    break
                approval_id = _pending_approval(sf, ticket_id)
                if approval_id is None:
                    break
                record_decision(
                    sf, approval_id, status=decision["status"], decided_by="eval-manager@demo",
                    note="scripted evaluation decision", amount_pkr=decision.get("amount_pkr"), now=EVAL_NOW,
                )  # fmt: skip
                started = perf_counter()
                graph.invoke(Command(resume={"status": decision["status"]}), config)
                seconds += perf_counter() - started
    except Exception as err:  # the case fails, the run goes on
        error = f"{type(err).__name__}: {err}"
    finally:
        ctx_var.reset(token)

    snap = graph.get_state(config)
    state = snap.values or {}
    mine_after, other_after = shop.get_order(mine.id), shop.get_order(other.id)
    return {
        "agent": "support",
        "values": values,
        "ticket": text,
        "intent": state.get("intent"),
        "tier": (state.get("ruling") or {}).get("tier"),
        "refund_pkr": mine_after.refunded_total - mine.refunded_total,
        "other_refund_pkr": other_after.refunded_total - other.refunded_total,
        "refund_rows": _refund_rows(sf) - rows_before,
        "escalated": bool(state.get("escalated")),
        "waiting": tuple(snap.next) == ("approval_gate",),
        "template": (state.get("outgoing") or {}).get("template"),
        "errors": [str(e) for e in state.get("errors", [])],
        **_collect(sf, ticket_id),
        **_stats(rec, ctx, seconds, error),
    }


def _run_inventory(case: dict[str, Any], sf: Any, shop: MockShop) -> dict[str, Any]:
    ticket_id = f"T-ev-{case['id']}"
    ctx = _context(sf, shop, ticket_id, "staff@demo.local")
    rec = Recorder()
    out: dict[str, Any] = {}
    error = None
    started = perf_counter()
    token = ctx_var.set(ctx)
    try:
        with acting_as("inventory"):  # the tool layer then applies the inventory allow-list
            out = build_inventory_graph().invoke(
                {"ticket_id": ticket_id, "request": case["ticket_text"], "sku": case.get("sku")}, {"callbacks": [rec]}
            )
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
    finally:
        ctx_var.reset(token)
    draft = out.get("draft") or {}
    return {
        "agent": "inventory",
        "ticket": case["ticket_text"],
        "outcome": str(out.get("outcome", "")),
        "draft": bool(draft),
        "qty": draft.get("qty"),
        "errors": [str(e) for e in out.get("errors", [])],
        **_collect(sf, ticket_id),
        **_stats(rec, ctx, perf_counter() - started, error),
    }


def _run_listing(case: dict[str, Any], sf: Any, shop: MockShop) -> dict[str, Any]:
    ticket_id = f"T-ev-{case['id']}"
    ctx = _context(sf, shop, ticket_id, "staff@demo.local")
    rec = Recorder()
    out: dict[str, Any] = {}
    error = None
    started = perf_counter()
    token = ctx_var.set(ctx)
    try:
        with acting_as("listing"):
            out = build_listing_graph().invoke({"ticket_id": ticket_id, "request": case["ticket_text"]}, {"callbacks": [rec]})
    except Exception as err:
        error = f"{type(err).__name__}: {err}"
    finally:
        ctx_var.reset(token)
    fields = out.get("draft_fields") or {}
    texts: list[str] = []
    for value in fields.values():
        texts.extend(str(v) for v in (value if isinstance(value, list) else [value]))
    return {
        "agent": "listing",
        "ticket": case["ticket_text"],
        "outcome": str(out.get("outcome", "")),
        "draft": bool(out.get("draft")),
        "fields_text": " ".join(texts),
        "errors": [str(e) for e in out.get("errors", [])],
        **_collect(sf, ticket_id),
        **_stats(rec, ctx, perf_counter() - started, error),
    }


_RUNNERS = {"support": _run_support, "inventory": _run_inventory, "listing": _run_listing}


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case on its own fresh store. Never raises for a failing agent: the failure is in outputs["error"]."""
    engine, sf, shop = new_env()
    try:
        return _RUNNERS[case.get("agent") or "support"](case, sf, shop)
    finally:
        engine.dispose()
