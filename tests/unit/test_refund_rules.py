"""Boundary and invariant tests for the refund policy engine (Step F). No database, no LLM."""
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shoppilot.policy.limits import Limits, load_limits
from shoppilot.policy.refund_rules import REF_COD, REF_DAMAGED, evaluate_refund

NOW = datetime(2026, 10, 10, 12, 0)


def make_order(**overrides):
    order = {
        "amount_paid": 2400,
        "refunded_total": 0,
        "delivered_at": NOW - timedelta(days=3),
        "is_overdue": False,
        "days_late": 0,
        "customer_flagged": False,
        "refunds_last_90d": 0,
        "payment_method": "prepaid",
        "has_open_refund": False,
        "non_refundable": False,
    }
    order.update(overrides)
    return order


def run(order=None, amount=2400, reason="other", evidence=(), **kwargs):
    return evaluate_refund(order or make_order(), amount, reason, evidence, NOW, **kwargs)


# --- amount boundaries (auto limit 3,000 and manager limit 15,000) ---------------------------------
@pytest.mark.parametrize(
    ("amount", "tier", "allowed"),
    [
        (1, "auto", 1),
        (2999, "auto", 2999),
        (3000, "auto", 3000),
        (3001, "manager", 3001),
        (14999, "manager", 14999),
        (15000, "manager", 15000),
        (15001, "owner", 15001),
    ],
)
def test_amount_boundaries(amount, tier, allowed):
    d = run(make_order(amount_paid=20_000), amount=amount)
    assert (d.tier, d.allowed_amount) == (tier, allowed)


# --- refund window: day 14 is still inside, day 15 is outside --------------------------------------
@pytest.mark.parametrize(
    ("delta", "tier"),
    [
        (timedelta(days=0), "auto"),
        (timedelta(days=13), "auto"),
        (timedelta(days=14), "auto"),
        (timedelta(days=14, hours=23), "auto"),
        (timedelta(days=15), "deny"),
        (timedelta(days=30), "deny"),
    ],
)
def test_window_boundaries(delta, tier):
    d = run(make_order(delivered_at=NOW - delta))
    assert d.tier == tier
    assert (d.allowed_amount == 0) == (tier == "deny")


# --- late delivery: more than 5 days late qualifies, exactly 5 does not -----------------------------
@pytest.mark.parametrize(
    ("days_late", "tier"),
    [(0, "deny"), (5, "deny"), (6, "auto"), (20, "auto")],
)
def test_late_threshold_after_window(days_late, tier):
    d = run(make_order(delivered_at=NOW - timedelta(days=30), days_late=days_late))
    assert d.tier == tier


# --- order not delivered yet -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overdue", "days_late", "tier"),
    [(False, 0, "deny"), (False, 10, "deny"), (True, 5, "deny"), (True, 6, "auto")],
)
def test_undelivered_orders(overdue, days_late, tier):
    d = run(make_order(delivered_at=None, is_overdue=overdue, days_late=days_late))
    assert d.tier == tier


def test_delivery_date_in_the_future_is_denied():
    assert run(make_order(delivered_at=NOW + timedelta(days=1))).tier == "deny"


# --- invalid requested amounts ---------------------------------------------------------------------
@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "1000"])
def test_invalid_amount_is_denied(bad):
    d = run(amount=bad)
    assert d.tier == "deny" and d.allowed_amount == 0


# --- hard invariants: money already refunded, open refund, non-refundable --------------------------
def test_fully_refunded_order_is_denied():
    assert run(make_order(refunded_total=2400)).tier == "deny"


def test_over_refunded_order_is_denied():
    assert run(make_order(refunded_total=3000)).tier == "deny"


def test_open_refund_blocks_a_second_one():
    assert run(make_order(has_open_refund=True)).tier == "deny"


def test_non_refundable_category_is_denied():
    assert run(make_order(non_refundable=True)).tier == "deny"


def test_request_above_paid_amount_is_reduced_and_needs_manager():
    d = run(make_order(amount_paid=2400), amount=5000)
    assert d.tier == "manager" and d.allowed_amount == 2400


def test_request_above_remaining_balance_is_reduced():
    d = run(make_order(amount_paid=5000, refunded_total=2000), amount=5000)
    assert d.tier == "manager" and d.allowed_amount == 3000


def test_earlier_refund_means_no_automatic_refund():
    d = run(make_order(amount_paid=5000, refunded_total=1000), amount=1000)
    assert d.tier == "manager"


# --- flagged and blocked customers -----------------------------------------------------------------
def test_flagged_customer_goes_to_owner():
    assert run(make_order(customer_flagged=True)).tier == "owner"


def test_blocked_customer_goes_to_owner():
    assert run(make_order(customer_blocked=True)).tier == "owner"


def test_flagged_customer_outside_window_is_still_denied():
    d = run(make_order(customer_flagged=True, delivered_at=NOW - timedelta(days=40)))
    assert d.tier == "deny"


# --- repeat refunders: two refunds in 90 days goes to owner ----------------------------------------
@pytest.mark.parametrize(("count", "tier"), [(0, "auto"), (1, "auto"), (2, "owner"), (3, "owner")])
def test_repeat_refunds(count, tier):
    assert run(make_order(refunds_last_90d=count)).tier == tier


# --- payment method --------------------------------------------------------------------------------
def test_cash_on_delivery_needs_manager():
    d = run(make_order(payment_method="cod"))
    assert d.tier == "manager"
    assert REF_COD in d.policy_refs


def test_unknown_payment_method_needs_manager():
    assert run(make_order(payment_method=None)).tier == "manager"


# --- damaged items ---------------------------------------------------------------------------------
def test_damaged_with_evidence_needs_manager():
    d = run(reason="damaged", evidence=("photo of broken screen",))
    assert d.tier == "manager"
    assert REF_DAMAGED in d.policy_refs


def test_damaged_outside_window_is_allowed_with_evidence():
    d = run(make_order(delivered_at=NOW - timedelta(days=30)), reason="damaged", evidence=("photo",))
    assert d.tier == "manager"


def test_damaged_without_evidence_is_denied():
    assert run(reason="damaged", evidence=()).tier == "deny"


def test_damaged_with_blank_evidence_is_denied():
    assert run(reason="damaged", evidence=("  ",)).tier == "deny"


def test_damaged_high_amount_goes_to_owner():
    d = run(make_order(amount_paid=20_000), amount=20_000, reason="damaged", evidence=("photo",))
    assert d.tier == "owner"


# --- store-wide daily cap for automatic refunds ----------------------------------------------------
@pytest.mark.parametrize(("today", "tier"), [(0, "auto"), (27_600, "auto"), (27_601, "manager"), (30_000, "manager")])
def test_daily_automatic_cap(today, tier):
    assert run(refunded_today_pkr=today).tier == tier


# --- limits come from configuration ----------------------------------------------------------------
def test_custom_limits_change_the_tier():
    assert run(amount=1500, limits=Limits(auto_refund_limit_pkr=1000)).tier == "manager"


def test_load_limits_from_yaml(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("limits:\n  auto_refund_limit_pkr: 1234\n  unknown_key: 1\n", encoding="utf-8")
    cfg = load_limits(f)
    assert cfg.auto_refund_limit_pkr == 1234
    assert cfg.manager_limit_pkr == Limits().manager_limit_pkr


def test_load_limits_default_file_has_the_blueprint_values():
    cfg = load_limits()
    assert (cfg.auto_refund_limit_pkr, cfg.manager_limit_pkr, cfg.refund_window_days) == (3000, 15000, 14)


# --- property test: the invariants hold for any input ----------------------------------------------
orders = st.fixed_dictionaries(
    {
        "amount_paid": st.integers(0, 50_000),
        "refunded_total": st.integers(0, 50_000),
        "delivered_at": st.one_of(st.none(), st.integers(-2, 40).map(lambda d: NOW - timedelta(days=d))),
        "is_overdue": st.booleans(),
        "days_late": st.integers(0, 20),
        "customer_flagged": st.booleans(),
        "refunds_last_90d": st.integers(0, 5),
        "payment_method": st.sampled_from(["cod", "prepaid"]),
        "has_open_refund": st.booleans(),
        "non_refundable": st.booleans(),
    }
)


@given(
    order=orders,
    amount=st.integers(-100, 60_000),
    reason=st.sampled_from(["late", "damaged", "other"]),
    evidence=st.sampled_from([(), ("photo",)]),
    today=st.integers(0, 40_000),
)
def test_invariants_hold_for_any_input(order, amount, reason, evidence, today):
    cfg = Limits()
    d = evaluate_refund(order, amount, reason, evidence, NOW, refunded_today_pkr=today)
    remaining = order["amount_paid"] - order["refunded_total"]
    assert d.reasons
    if d.tier == "deny":
        assert d.allowed_amount == 0
        return
    assert 0 < d.allowed_amount <= min(amount, remaining)
    assert not order["has_open_refund"] and not order["non_refundable"]
    if order["customer_flagged"]:
        assert d.tier == "owner"
    if d.tier == "auto":
        assert d.allowed_amount <= cfg.auto_refund_limit_pkr
        assert order["payment_method"] == "prepaid"
        assert order["refunded_total"] == 0
        assert reason != "damaged"
        assert order["refunds_last_90d"] < cfg.repeat_refund_count
        assert today + d.allowed_amount <= cfg.auto_refunds_per_day_pkr
