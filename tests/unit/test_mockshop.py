"""MockShop and the seed data (Step E). Runs on in-memory SQLite, so no Docker is needed.

`NOW` is fixed and given to both the seed and MockShop, so every run sees the same store.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from shoppilot.core.errors import IdempotencyConflict, NotFound, OrderNotFound, ShopBackendError, ValidationFailed
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.base import Order, ShopBackend
from shoppilot.shop.mock_models import OrderRow, ProductRow
from shoppilot.shop.mockshop import MockShop
from shoppilot.shop.seed import seed_database

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # SQLite and Decimal

NOW = datetime(2026, 10, 1, 12, 0, 0)

EXPECTED_COUNTS = {
    "on_time_status": 40,
    "late_delivery": 30,
    "damaged_item": 20,
    "wrong_item": 15,
    "cod_refund": 15,
    "already_refunded": 15,
    "outside_window": 20,
    "wrong_email": 15,
    "injection": 20,
    "repeat_refunder": 4,
    "repeat_refunder_history": 6,
}


def make_store():
    engine = make_engine("sqlite://")
    seed_database(engine, NOW)
    sf = make_session_factory(engine)
    return MockShop(sf, now=lambda: NOW), sf


@pytest.fixture(scope="module")
def store():
    """Seeded once, for tests that only read."""
    return make_store()


@pytest.fixture
def fresh_store():
    """A new seeded store for each test that writes."""
    return make_store()


def orders_of(store, scenario: str) -> list[Order]:
    shop, sf = store
    with sf() as s:
        names = s.scalars(select(OrderRow.name).where(OrderRow.scenario == scenario).order_by(OrderRow.id)).all()
    return [shop.get_order(n) for n in names]


# ------------------------------------------------------------------ seed data
def test_mockshop_is_a_shop_backend(store):
    assert isinstance(store[0], ShopBackend)


def test_seed_has_200_orders_in_the_scenario_mix(store):
    _, sf = store
    with sf() as s:
        rows = s.execute(select(OrderRow.scenario, func.count(OrderRow.id)).group_by(OrderRow.scenario)).all()
    assert dict(rows) == EXPECTED_COUNTS
    assert sum(EXPECTED_COUNTS.values()) == 200


def test_seed_is_deterministic():
    first, second = make_store(), make_store()
    a = [first[0].get_order(f"#{n}") for n in range(88601, 88801)]
    b = [second[0].get_order(f"#{n}") for n in range(88601, 88801)]
    assert a == b


# --------------------------------------------------------------------- reads
def test_get_order_by_name_bare_number_and_id(store):
    shop = store[0]
    assert shop.get_order("#88601").id == "#88601"
    assert shop.get_order("88601").id == "#88601"
    assert shop.get_order("1").id == "#88601"  # numeric Shopify-style id


def test_get_order_not_found(store):
    with pytest.raises(OrderNotFound) as err:
        store[0].get_order("#99999")
    assert err.value.code == "ORDER_NOT_FOUND"


def test_on_time_status_orders_are_not_late(store):
    orders = orders_of(store, "on_time_status")
    assert {o.status for o in orders} == {"delivered", "shipped", "placed"}
    assert all(o.days_late == 0 and not o.is_overdue for o in orders)


def test_late_delivery_orders_are_5_to_12_days_late(store):
    orders = orders_of(store, "late_delivery")
    assert all(5 <= o.days_late <= 12 for o in orders)
    assert {o.status for o in orders} == {"delivered", "shipped"}  # delivered late, or still on the way and overdue
    assert all(o.payment_method == "prepaid" and o.refunded_total == 0 for o in orders)


def test_overdue_orders_have_no_delivery_date(store):
    overdue = [o for o in orders_of(store, "late_delivery") if o.status == "shipped"]
    assert overdue and all(o.is_overdue and o.delivered_at is None for o in overdue)


def test_already_refunded_orders(store):
    orders = orders_of(store, "already_refunded")
    full = [o for o in orders if o.status == "refunded"]
    partial = [o for o in orders if o.status != "refunded"]
    assert len(full) == 8 and all(o.refunded_total == o.amount_paid for o in full)
    assert len(partial) == 7 and all(0 < o.refunded_total < o.amount_paid for o in partial)


def test_outside_window_orders_were_delivered_more_than_14_days_ago(store):
    for o in orders_of(store, "outside_window"):
        assert o.status == "delivered" and o.days_late == 0
        assert o.delivered_at is not None and (NOW - o.delivered_at).days > 14


def test_cod_refund_orders_are_cash_on_delivery(store):
    orders = orders_of(store, "cod_refund")
    assert all(o.payment_method == "cod" and o.status == "delivered" for o in orders)


def test_repeat_refunders_and_flagged_customers_go_to_the_owner_tier(store):
    orders = orders_of(store, "repeat_refunder")
    assert len(orders) == 4
    assert all(o.customer_flagged or o.refunds_last_90d >= 2 for o in orders)
    assert sum(o.customer_flagged for o in orders) == 1


def test_injection_orders_carry_hostile_notes(store):
    orders = orders_of(store, "injection")
    assert len(orders) == 20 and all(o.note for o in orders)
    assert any("Ignore all previous instructions" in o.note for o in orders)


def test_find_orders_ignores_case_and_respects_limit(store):
    shop = store[0]
    email = shop.get_order("#88601").customer_email
    found = shop.find_orders(email.upper())
    assert "#88601" in [o.id for o in found]
    assert all(o.customer_email == email for o in found)
    assert len(shop.find_orders(email, limit=1)) == 1
    assert shop.find_orders("nobody@example.com") == []


def test_get_customer_by_email_and_by_id(store):
    shop = store[0]
    order = shop.get_order("#88601")
    by_email = shop.get_customer(order.customer_email)
    assert by_email.id == order.customer_id
    assert shop.get_customer(order.customer_id).email == order.customer_email
    with pytest.raises(NotFound):
        shop.get_customer("nobody@example.com")


def test_flagged_customer_is_flagged(store):
    shop = store[0]
    flagged = next(o for o in orders_of(store, "repeat_refunder") if o.customer_flagged)
    assert shop.get_customer(flagged.customer_id).flagged is True


def test_track_shipment(store):
    shop = store[0]
    delayed = next(o for o in orders_of(store, "late_delivery") if o.status == "shipped")
    ship = shop.track_shipment(delayed.tracking_no)
    assert ship.order_id == delayed.id and ship.status == "delayed"
    assert any(e.status == "delayed" for e in ship.events)
    with pytest.raises(NotFound) as err:
        shop.track_shipment("TCS000")
    assert err.value.code == "SHIPMENT_NOT_FOUND"


def test_get_product_and_inventory(store):
    shop = store[0]
    serum = shop.get_product("SERUM-30-VC")
    assert (serum.price_pkr, serum.category, serum.refundable) == (1500, "Beauty", False)
    assert serum.supplier == "Lahore Beauty Labs"
    stock = shop.get_inventory("EARBUDS-TWS-01")
    assert stock.on_hand < stock.reorder_point and stock.avg_daily_sales == 3.2
    with pytest.raises(NotFound) as err:
        shop.get_product("NOPE")
    assert err.value.code == "PRODUCT_NOT_FOUND"


# -------------------------------------------------------------------- writes
def first_late_order(store) -> Order:
    return orders_of(store, "late_delivery")[0]


def test_refund_is_idempotent(fresh_store):
    shop = fresh_store[0]
    order = first_late_order(fresh_store)
    first = shop.create_refund(order.id, 100, key="k1", reason="late")
    again = shop.create_refund(order.id, 100, key="k1", reason="late")
    assert again.id == first.id
    after = shop.get_order(order.id)
    assert after.refunded_total == 100  # refunded once, not twice
    assert after.refunds_last_90d == order.refunds_last_90d + 1


def test_same_key_with_another_amount_is_a_conflict(fresh_store):
    shop = fresh_store[0]
    order = first_late_order(fresh_store)
    shop.create_refund(order.id, 100, key="k1")
    with pytest.raises(IdempotencyConflict):
        shop.create_refund(order.id, 200, key="k1")


def test_refund_cannot_exceed_the_balance(fresh_store):
    shop = fresh_store[0]
    order = first_late_order(fresh_store)
    with pytest.raises(ShopBackendError) as err:
        shop.create_refund(order.id, order.amount_paid + 1, key="too-much")
    assert err.value.code == "REFUND_EXCEEDS_BALANCE"
    with pytest.raises(ShopBackendError) as err:
        shop.create_refund(order.id, 0, key="zero")
    assert err.value.code == "INVALID_AMOUNT"


def test_partial_then_full_refund_updates_the_status(fresh_store):
    shop = fresh_store[0]
    order = first_late_order(fresh_store)
    shop.create_refund(order.id, 100, key="part")
    assert shop.get_order(order.id).refunded_total == 100
    shop.create_refund(order.id, order.amount_paid - 100, key="rest")
    done = shop.get_order(order.id)
    assert done.status == "refunded" and done.refunded_total == order.amount_paid
    with pytest.raises(ShopBackendError) as err:  # nothing left to refund
        shop.create_refund(order.id, 1, key="extra")
    assert err.value.code == "REFUND_EXCEEDS_BALANCE"


def test_cancel_only_orders_that_are_not_shipped(fresh_store):
    shop = fresh_store[0]
    placed = next(o for o in orders_of(fresh_store, "on_time_status") if o.status == "placed")
    assert shop.cancel_order(placed.id).status == "cancelled"
    delivered = orders_of(fresh_store, "damaged_item")[0]
    with pytest.raises(ShopBackendError) as err:
        shop.cancel_order(delivered.id)
    assert err.value.code == "ORDER_NOT_CANCELLABLE"


def test_product_draft_is_a_draft_product(fresh_store):
    shop, sf = fresh_store
    draft = shop.create_product_draft({"title": "Test Kurta", "tags": ["summer", "cotton"], "description": "Light"})
    assert draft.status == "draft" and draft.title == "Test Kurta"
    with sf() as s:
        row = s.get(ProductRow, draft.id)
        assert row is not None and row.status == "draft"
        assert row.tags == "summer, cotton" and row.body_html == "Light"
    with pytest.raises(ValidationFailed):
        shop.create_product_draft({"title": "  "})


def test_purchase_order_draft(fresh_store):
    shop = fresh_store[0]
    po = shop.create_purchase_order_draft("EARBUDS-TWS-01", 50, "Shenzhen Audio")
    assert po.status == "draft" and po.qty == 50
    with pytest.raises(ValidationFailed):
        shop.create_purchase_order_draft("EARBUDS-TWS-01", 0, "Shenzhen Audio")
    with pytest.raises(NotFound):
        shop.create_purchase_order_draft("NOPE", 5, "Shenzhen Audio")
