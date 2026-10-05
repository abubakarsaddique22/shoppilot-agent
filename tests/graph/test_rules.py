"""Step K: the rules node. The real policy engine runs on an in-memory MockShop. No model, no Docker, no internet.

The model proposed something in `decide`. `rules` asks the engine, and the engine always has the last word.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from shoppilot.agents.support import NO_RULING, rules
from shoppilot.core.config import settings
from shoppilot.db.models import AuditLogRow

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # SQLite and Decimal


def refund_proposal(amount: int = 500, reason: str = "late") -> dict:
    return {"action": "refund", "amount_pkr": amount, "reason": reason, "evidence_ids": ["order:x"], "summary": "x"}


def make_state(order_id: str, proposal: dict, text: str = "refund please", **changes) -> dict:
    state = {"intent": "refund", "order": {"id": order_id}, "proposal": proposal, "messages": [HumanMessage(text)]}
    state.update(changes)
    return state


def overrides(env) -> list[AuditLogRow]:
    with env.sf() as s:
        return list(s.scalars(select(AuditLogRow).where(AuditLogRow.event == "policy_override")))


# ------------------------------------------------------------------- tiers
def test_a_small_late_refund_is_auto(env):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    ruling = rules(make_state(late.id, refund_proposal(500)))["ruling"]
    assert ruling["tier"] == "auto" and ruling["allowed_amount"] == 500 and ruling["proposed_amount"] == 500
    assert ruling["overridden"] is False and ruling["policy_refs"]
    assert overrides(env) == []  # no disagreement, nothing to log
    assert ctx.reads_used == 1  # one read for the engine


def test_cash_on_delivery_needs_a_manager(env):
    cod = env.orders_of("cod_refund")[0]
    env.start(cod.customer_email)
    ruling = rules(make_state(cod.id, refund_proposal(500)))["ruling"]
    assert ruling["tier"] == "manager" and ruling["allowed_amount"] == 500 and ruling["overridden"] is False


def test_a_repeat_refunder_needs_the_owner(env):
    repeat = env.orders_of("repeat_refunder")[0]
    env.start(repeat.customer_email)
    assert rules(make_state(repeat.id, refund_proposal(500)))["ruling"]["tier"] == "owner"


# ------------------------------------------------------ the engine wins
def test_a_refund_the_policy_forbids_is_denied_and_the_disagreement_is_logged(env):
    old = env.orders_of("outside_window")[0]
    env.start(old.customer_email)
    ruling = rules(make_state(old.id, refund_proposal(500)))["ruling"]
    assert ruling["tier"] == "deny" and ruling["allowed_amount"] == 0 and ruling["overridden"] is True
    assert ruling["reasons"] and ruling["policy_refs"]
    [row] = overrides(env)
    assert row.detail_json["tier"] == "deny" and row.detail_json["proposed_amount"] == 500
    assert row.detail_json["ticket_id"] == "T-1"


def test_an_amount_above_the_refundable_balance_is_lowered(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    ruling = rules(make_state(late.id, refund_proposal(50_000)))["ruling"]
    assert ruling["allowed_amount"] == late.amount_paid - late.refunded_total < 50_000
    assert ruling["proposed_amount"] == 50_000 and ruling["overridden"] is True
    assert ruling["tier"] in ("manager", "owner")  # a lowered amount is never silently automatic
    assert len(overrides(env)) == 1


def test_a_damage_claim_with_the_customers_description_goes_to_a_manager(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    state = make_state(late.id, refund_proposal(500, "damaged"), text="The cup arrived broken, photo attached")
    assert rules(state)["ruling"]["tier"] == "manager"


def test_a_damage_claim_without_any_description_is_denied(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    assert rules(make_state(late.id, refund_proposal(500, "damaged"), text="   "))["ruling"]["tier"] == "deny"


def test_the_rules_node_never_moves_money(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    rules(make_state(late.id, refund_proposal(500)))
    assert env.shop.get_order(late.id).refunded_total == late.refunded_total


# ------------------------------------------------ no money, no engine call
@pytest.mark.parametrize("action", ["reply", "escalate"])
def test_a_reply_or_an_escalation_needs_no_ruling(env, action):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    result = rules(make_state(late.id, {"action": action, "amount_pkr": None, "reason": None}))
    assert result == {"ruling": NO_RULING}
    assert ctx.reads_used == 0  # the engine was not asked


def test_a_missing_proposal_needs_no_ruling(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    assert rules({"intent": "refund", "order": {"id": late.id}}) == {"ruling": NO_RULING}


def test_the_shared_no_ruling_is_not_changed_by_a_run(env):
    late = env.orders_of("late_delivery")[0]
    env.start(late.customer_email)
    rules(make_state(late.id, {"action": "reply"}))["ruling"]["tier"] = "auto"
    assert NO_RULING["tier"] == "none"


# ----------------------------------------- the engine cannot be asked: safe exit
def test_a_spent_read_budget_turns_the_refund_into_an_escalation(env):
    late = env.orders_of("late_delivery")[0]
    ctx = env.start(late.customer_email)
    ctx.reads_used = settings.max_tool_calls
    result = rules(make_state(late.id, refund_proposal(500), errors=["NO_POLICY_FOUND"]))
    assert result["proposal"]["action"] == "escalate" and result["proposal"]["amount_pkr"] is None
    assert result["ruling"]["tier"] == "none" and result["ruling"]["error"] == "BUDGET_EXCEEDED"
    assert result["errors"] == ["NO_POLICY_FOUND", "BUDGET_EXCEEDED"]  # earlier errors are kept


def test_an_order_of_another_customer_is_never_ruled_on(env):
    mine, theirs = env.orders_of("late_delivery")[0], env.orders_of("on_time_status")[0]
    assert mine.customer_email != theirs.customer_email
    env.start(mine.customer_email)
    result = rules(make_state(theirs.id, refund_proposal(500)))
    assert result["proposal"]["action"] == "escalate" and result["errors"] == ["ORDER_NOT_FOUND"]
    assert result["ruling"]["tier"] == "none"
