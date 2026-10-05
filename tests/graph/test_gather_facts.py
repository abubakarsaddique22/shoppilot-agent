"""Step K: the gather_facts node. Real tools on an in-memory MockShop; only the policy search is faked (it needs pgvector).

No Docker, no model download, no API key.
"""
from __future__ import annotations

import json

import pytest

from shoppilot.agents.support import MAX_POLICY_HITS, POLICY_QUERIES, gather_facts
from shoppilot.core.config import settings
from shoppilot.core.errors import ShopBackendError
from shoppilot.kb.retriever import PolicyHit, PolicySearchResult
from shoppilot.tools import orders as orders_module

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # SQLite and Decimal

LATE_SECTION = "Returns > Late deliveries"


def hit(section: str = LATE_SECTION, text: str = "A refund is possible when the order is more than 5 days late.") -> PolicyHit:
    return PolicyHit(doc="returns", section=section, text=text, version="v1", score=0.9)


@pytest.fixture
def policy(monkeypatch):
    """Fake policy search. policy.queries records every query; policy.result is what the search returns."""
    state = type("PolicyFake", (), {})()
    state.queries = []
    state.result = PolicySearchResult(ok=True, hits=[hit()])

    def fake(session, query):
        state.queries.append(query)
        return state.result

    monkeypatch.setattr(orders_module, "kb_search", fake)
    return state


def refund_state(order_id: str, intent: str = "refund") -> dict:
    return {"intent": intent, "order_ref": order_id}


def test_a_refund_ticket_collects_order_tracking_and_policy_facts(env, policy):
    late = env.orders_of("late_delivery")[0]
    assert late.tracking_no
    ctx = env.start(late.customer_email)
    result = gather_facts(refund_state(late.id))
    assert result["errors"] == []
    assert result["order"]["id"] == late.id
    ids = [f["id"] for f in result["facts"]]
    assert ids == [f"order:{late.id}", f"tracking:{late.tracking_no}", f"policy:{LATE_SECTION}"]
    assert [f["source"] for f in result["facts"]] == ["get_order", "track_shipment", "search_policy"]
    assert ctx.reads_used == 3  # within the budget of 6 reads


def test_the_policy_query_is_fixed_text_per_intent(env, policy):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    gather_facts(refund_state(late.id, intent="refund"))
    assert policy.queries == [POLICY_QUERIES["refund"]]


def test_an_intent_without_a_policy_query_skips_the_search(env, policy):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    result = gather_facts(refund_state(late.id, intent="other"))
    assert policy.queries == []
    assert not [f for f in result["facts"] if f["source"] == "search_policy"]


def test_only_a_few_policy_hits_are_kept(env, policy):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    policy.result = PolicySearchResult(ok=True, hits=[hit(f"Returns > Section {n}") for n in range(MAX_POLICY_HITS + 2)])
    result = gather_facts(refund_state(late.id))
    assert len([f for f in result["facts"] if f["source"] == "search_policy"]) == MAX_POLICY_HITS


def test_without_an_order_number_no_tool_is_called(env, policy):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    assert gather_facts({"intent": "order_status", "order_ref": None}) == {"facts": [], "errors": ["NO_ORDER_REF"]}
    assert ctx.reads_used == 0 and policy.queries == []


def test_another_customers_order_looks_like_a_missing_order(env, policy):
    mine, theirs = env.orders_of("late_delivery")[0], env.orders_of("on_time_status")[0]
    assert mine.customer_email != theirs.customer_email
    env.start(mine.customer_email)
    other = gather_facts(refund_state(theirs.id))
    missing = gather_facts(refund_state("#99999"))
    assert other == missing == {"facts": [], "errors": ["ORDER_NOT_FOUND"]}
    assert policy.queries == []  # nothing else is read for an order that is not theirs


def test_a_hostile_order_note_never_reaches_the_facts(env, policy):
    injected = env.orders_of("injection")[0]
    env.start(injected.customer_email)
    result = gather_facts(refund_state(injected.id))
    assert result["order"]["id"] == injected.id
    assert "Ignore all previous instructions" not in json.dumps(result)


def test_a_courier_outage_gives_an_unknown_tracking_fact_not_an_error(env, policy, monkeypatch):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)

    def courier_down(tracking_no):
        raise ShopBackendError("courier timeout")

    monkeypatch.setattr(env.shop, "track_shipment", courier_down)
    result = gather_facts(refund_state(late.id))
    assert result["errors"] == []
    tracking = next(f for f in result["facts"] if f["source"] == "track_shipment")
    assert tracking["data"]["status"] == "unknown"


def test_no_policy_found_keeps_the_other_facts_and_reports_the_error(env, policy):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    policy.result = PolicySearchResult(ok=False, error="NO_POLICY_FOUND")
    result = gather_facts(refund_state(late.id))
    assert result["errors"] == ["NO_POLICY_FOUND"]
    assert result["facts"][0]["id"] == f"order:{late.id}"  # the order fact is still there


def test_a_spent_read_budget_stops_with_the_partial_facts(env, policy):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    ctx.reads_used = settings.max_tool_calls - 1  # one read left: get_order takes it
    result = gather_facts(refund_state(late.id))
    assert result["errors"] == ["BUDGET_EXCEEDED"]
    assert [f["source"] for f in result["facts"]] == ["get_order"]
    assert policy.queries == []


def test_a_budget_already_spent_before_get_order_is_reported(env, policy):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    ctx.reads_used = settings.max_tool_calls
    assert gather_facts(refund_state(late.id)) == {"facts": [], "errors": ["BUDGET_EXCEEDED"]}
