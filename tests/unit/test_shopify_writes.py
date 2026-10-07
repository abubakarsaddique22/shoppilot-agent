"""ShopifyBackend: sales_summary, product draft, purchase order draft, refund and cancel (Step E, part 2).

Same idea as test_shopify_backend.py: a fake Shopify (httpx.MockTransport), no network and no secrets.
Each test gives the fake a few "rules": if the query text contains the needle, the rule answers.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx
import pytest

from shoppilot.core.errors import ConfigError, IdempotencyConflict, NotFound, ShopBackendError, ValidationFailed
from shoppilot.db.models import AppBase, PurchaseDraftRow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.shopify import ShopifyBackend

NOW = datetime(2026, 10, 1, 12, 0, 0)
PAGE_END = {"hasNextPage": False, "endCursor": None}

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def money(amount: str) -> dict:
    return {"shopMoney": {"amount": amount}}


class Scripted:
    """A fake Shopify. rules = [(needle, handler)]: the first needle found in the query text answers."""

    def __init__(self, rules: list[tuple[str, Handler]]) -> None:
        self.rules = rules
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "t-1", "scope": "x", "expires_in": 86399})
        body = json.loads(request.content)
        self.calls.append((body["query"], body["variables"]))
        for needle, handler in self.rules:
            if needle in body["query"]:
                return httpx.Response(200, json={"data": handler(body["variables"])})
        return httpx.Response(200, json={"errors": [{"message": "the test has no rule for this query"}]})

    def sent(self, needle: str) -> list[dict[str, Any]]:
        """The variables of every call whose query contains the needle."""
        return [variables for query, variables in self.calls if needle in query]


def static(data: dict[str, Any]) -> Handler:
    return lambda _variables: data


def make_backend(fake: Scripted, session_factory: Any = None) -> ShopifyBackend:
    return ShopifyBackend(
        domain="demo.myshopify.com",
        client_id="id",
        client_secret="secret",
        now=lambda: NOW,
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        sleep=lambda _s: None,
        session_factory=session_factory,
    )


# ---------------------------------------------------------------------------------------------- sales_summary
def tx(kind: str, status: str, amount: str, at: str) -> dict:
    return {"kind": kind, "status": status, "createdAt": at, "amountSet": money(amount)}


def day_order(name: str, created: str, amount: str, cancelled: str | None = None, txs: list | None = None) -> dict:
    return {
        "name": name,
        "createdAt": created,
        "cancelledAt": cancelled,
        "totalPriceSet": money(amount),
        "transactions": txs or [],
    }


def day_orders(variables: dict[str, Any]) -> dict[str, Any]:
    if "created_at" in variables["q"]:  # the orders of the day
        nodes = [
            day_order("#1", "2026-10-01T08:00:00Z", "5400.0"),  # counts
            day_order("#2", "2026-10-01T09:00:00Z", "2000.0", cancelled="2026-10-01T10:00:00Z"),  # cancelled
            day_order("#3", "2026-09-30T23:00:00Z", "700.0"),  # the day before: a loose search hit
        ]
    else:  # orders changed since the start of the day: look at their refund transactions
        nodes = [
            day_order(
                "#4",
                "2026-09-20T08:00:00Z",
                "3000.0",
                txs=[
                    tx("SALE", "SUCCESS", "3000.0", "2026-09-20T08:00:00Z"),
                    tx("REFUND", "SUCCESS", "1000.0", "2026-10-01T09:00:00Z"),  # counts
                    tx("REFUND", "SUCCESS", "500.0", "2026-09-30T09:00:00Z"),  # yesterday
                    tx("REFUND", "PENDING", "200.0", "2026-10-01T09:30:00Z"),  # not successful
                ],
            )
        ]
    return {"orders": {"nodes": nodes, "pageInfo": PAGE_END}}


def shipped(name: str, display: str, delivered: str | None, estimated: str, cancelled: str | None = None) -> dict:
    return {
        "name": name,
        "cancelledAt": cancelled,
        "displayFinancialStatus": "PAID",
        "fulfillments": [
            {"status": "SUCCESS", "displayStatus": display, "deliveredAt": delivered, "estimatedDeliveryAt": estimated}
        ],
    }


def variant(sku: str, reorder: str | None, on_hand: int) -> dict:
    return {
        "sku": sku,
        "reorderPoint": {"value": reorder} if reorder else None,
        "inventoryItem": {"inventoryLevels": {"nodes": [{"quantities": [{"name": "available", "quantity": on_hand}]}]}},
    }


def variant_pages(variables: dict[str, Any]) -> dict[str, Any]:
    if variables["after"] is None:  # first page, and there is a second one
        page = {"nodes": [variant("SKU-A", "5", 3)], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}
    else:
        page = {
            "nodes": [
                variant("SKU-B", "5", 10),  # enough stock
                variant("SKU-C", None, 0),  # no reorder_point metafield: never low
                variant("SKU-D", "10", 10),  # equal to the reorder point: low
            ],
            "pageInfo": PAGE_END,
        }
    return {"productVariants": page}


def test_sales_summary_numbers_of_one_day():
    shipped_nodes = [
        shipped("#5", "IN_TRANSIT", None, "2026-09-28T12:00:00Z"),  # late
        shipped("#6", "DELIVERED", "2026-09-27T12:00:00Z", "2026-09-28T12:00:00Z"),  # delivered
        shipped("#7", "IN_TRANSIT", None, "2026-10-05T12:00:00Z"),  # not late yet
        shipped("#8", "IN_TRANSIT", None, "2026-09-28T12:00:00Z", cancelled="2026-09-29T12:00:00Z"),  # cancelled
    ]
    fake = Scripted(
        [
            ("productVariants", variant_pages),
            ("after: $after, sortKey", static({"orders": {"nodes": shipped_nodes, "pageInfo": PAGE_END}})),
            ("transactions(first: 10)", day_orders),
        ]
    )
    summary = make_backend(fake).sales_summary(date(2026, 10, 1))

    assert summary.orders_count == 1 and summary.sales_pkr == 5400
    assert summary.refunds_count == 1 and summary.refunds_pkr == 1000
    assert summary.late_orders == 1
    assert [(i.sku, i.on_hand, i.reorder_point) for i in summary.low_stock] == [("SKU-A", 3, 5), ("SKU-D", 10, 10)]
    assert len(fake.sent("productVariants")) == 2  # both pages were read


# ---------------------------------------------------------------------------------------------- product draft
PRODUCT_ANSWER = {
    "productCreate": {
        "product": {
            "id": "gid://shopify/Product/555",
            "title": "Lawn Suit",
            "status": "DRAFT",
            "createdAt": "2026-10-01T11:00:00Z",
        },
        "userErrors": [],
    }
}


def test_product_draft_is_always_a_draft():
    fake = Scripted([("productCreate", static(PRODUCT_ANSWER))])
    fields = {
        "title": "Lawn Suit",
        "body_html": "<p>x</p>",
        "tags": ["new", "lawn"],
        "vendor": "V",
        "product_type": "Suit",
        "status": "ACTIVE",  # the agent must not be able to publish
    }
    draft = make_backend(fake).create_product_draft(fields)

    assert draft.id == 555 and draft.title == "Lawn Suit" and draft.status == "draft"
    sent = fake.sent("productCreate")[0]["product"]
    assert sent["status"] == "DRAFT"
    assert sent["tags"] == ["new", "lawn"] and sent["descriptionHtml"] == "<p>x</p>" and sent["productType"] == "Suit"


def test_product_draft_needs_a_title_and_reports_shopify_errors():
    with pytest.raises(ValidationFailed):
        make_backend(Scripted([])).create_product_draft({"title": "  "})
    refused = {"productCreate": {"product": None, "userErrors": [{"field": ["title"], "message": "too long"}]}}
    with pytest.raises(ShopBackendError) as err:
        make_backend(Scripted([("productCreate", static(refused))])).create_product_draft({"title": "X"})
    assert err.value.code == "PRODUCT_REJECTED"


# ---------------------------------------------------------------------------------------------- purchase order draft
def sqlite_sessions():
    engine = make_engine("sqlite://")
    AppBase.metadata.create_all(engine)
    return make_session_factory(engine)


def variant_lookup(nodes: list[dict]) -> Scripted:
    return Scripted([("productVariants", static({"productVariants": {"nodes": nodes}}))])


def test_purchase_order_draft_is_saved_in_our_own_table():
    sf = sqlite_sessions()
    backend = make_backend(variant_lookup([{"sku": "LAWN-3P-BLU"}]), sf)
    draft = backend.create_purchase_order_draft("lawn-3p-blu", 40, " Lahore Textiles ")

    assert draft.sku == "LAWN-3P-BLU" and draft.qty == 40 and draft.supplier == "Lahore Textiles"
    assert draft.status == "draft"
    with sf() as s:
        rows = s.query(PurchaseDraftRow).all()
    assert [(r.sku, r.qty, r.supplier) for r in rows] == [("LAWN-3P-BLU", 40, "Lahore Textiles")]


def test_purchase_order_draft_checks_before_it_writes():
    sf = sqlite_sessions()
    with pytest.raises(NotFound):  # the SKU is not in the store
        make_backend(variant_lookup([]), sf).create_purchase_order_draft("NOPE", 5, "S")
    with pytest.raises(ValidationFailed):
        make_backend(variant_lookup([{"sku": "A"}]), sf).create_purchase_order_draft("A", 0, "S")
    with pytest.raises(ValidationFailed):
        make_backend(variant_lookup([{"sku": "A"}]), sf).create_purchase_order_draft("A", 5, "  ")
    with pytest.raises(ConfigError):  # no database session was given
        make_backend(variant_lookup([{"sku": "A"}])).create_purchase_order_draft("A", 5, "S")
    with sf() as s:
        assert s.query(PurchaseDraftRow).count() == 0


# ---------------------------------------------------------------------------------------------- refund and cancel
PLACED = {
    "id": "gid://shopify/Order/1001",
    "name": "#1001",
    "email": "ali.khan@example.com",
    "createdAt": "2026-09-10T08:00:00Z",
    "processedAt": "2026-09-10T08:00:00Z",
    "cancelledAt": None,
    "note": "",
    "displayFinancialStatus": "PAID",
    "paymentGatewayNames": ["JazzCash"],
    "totalPriceSet": money("5400.0"),
    "customer": {"id": "gid://shopify/Customer/77", "tags": []},
    "lineItems": {"nodes": []},
    "fulfillments": [],
    "transactions": [
        {
            "id": "gid://shopify/OrderTransaction/9",
            "kind": "SALE",
            "status": "SUCCESS",
            "gateway": "JazzCash",
            "amountSet": money("5400.0"),
            "createdAt": "2026-09-10T08:00:00Z",
        }
    ],
}
DELIVERED = {
    **PLACED,
    "fulfillments": [
        {
            "id": "gid://shopify/Fulfillment/1",
            "status": "SUCCESS",
            "displayStatus": "DELIVERED",
            "deliveredAt": "2026-09-20T12:00:00Z",
            "estimatedDeliveryAt": "2026-09-15T12:00:00Z",
            "updatedAt": "2026-09-20T12:00:00Z",
            "trackingInfo": [{"number": "TCS1", "company": "TCS"}],
            "events": {"nodes": []},
        }
    ],
}
REFUND_NODE = {
    "id": "gid://shopify/Refund/31",
    "createdAt": "2026-10-01T11:00:00Z",
    "note": "late delivery [shoppilot:k1]",
    "totalRefundedSet": money("1000.0"),
    "transactions": {"nodes": [{"status": "SUCCESS"}]},
}


def refund_rules(order: dict, earlier_refunds: list[dict]) -> list[tuple[str, Handler]]:
    return [
        ("refundCreate", static({"refundCreate": {"refund": REFUND_NODE, "userErrors": []}})),
        ("order(id:", static({"order": {"refunds": earlier_refunds}})),
        ("customer(id", static({"customer": {"orders": {"nodes": []}}})),
        ("orders(first: $n", static({"orders": {"nodes": [order]}})),
    ]


def test_create_refund_sends_the_key_and_the_payment_to_shopify():
    fake = Scripted(refund_rules(PLACED, []))
    refund = make_backend(fake).create_refund("#1001", 1000, key="k1", reason="late delivery")

    assert refund.id == 31 and refund.order_id == "#1001" and refund.amount_pkr == 1000
    assert refund.reason == "late delivery" and refund.status == "issued" and refund.idempotency_key == "k1"
    variables = fake.sent("refundCreate")[0]
    transaction = variables["input"]["transactions"][0]
    assert transaction["parentId"] == "gid://shopify/OrderTransaction/9" and transaction["amount"] == "1000"
    assert transaction["kind"] == "REFUND" and transaction["gateway"] == "JazzCash"
    assert "[shoppilot:k1]" in variables["input"]["note"]
    assert variables["key"]  # Shopify's own idempotency key is sent too


def test_same_key_again_returns_the_earlier_refund_and_does_not_refund_twice():
    fake = Scripted(refund_rules(PLACED, [REFUND_NODE]))
    backend = make_backend(fake)
    again = backend.create_refund("#1001", 1000, key="k1")
    assert again.id == 31 and again.amount_pkr == 1000
    assert fake.sent("refundCreate") == []  # no second refund was sent
    with pytest.raises(IdempotencyConflict):  # same key, different amount
        backend.create_refund("#1001", 500, key="k1")


def test_refund_is_never_above_the_balance_and_needs_a_valid_request():
    fake = Scripted(refund_rules(PLACED, []))
    backend = make_backend(fake)
    with pytest.raises(ShopBackendError) as err:
        backend.create_refund("#1001", 9999, key="k2")
    assert err.value.code == "REFUND_EXCEEDS_BALANCE"
    with pytest.raises(ShopBackendError) as err:
        backend.create_refund("#1001", 0, key="k3")
    assert err.value.code == "INVALID_AMOUNT"
    with pytest.raises(ShopBackendError) as err:
        backend.create_refund("#1001", 100, key="  ")
    assert err.value.code == "INVALID_KEY"
    assert fake.sent("refundCreate") == []


def test_refund_rejected_by_shopify_becomes_an_error():
    rules = refund_rules(PLACED, [])
    refused = {"refundCreate": {"refund": None, "userErrors": [{"field": ["x"], "message": "cannot refund"}]}}
    rules[0] = ("refundCreate", static(refused))
    with pytest.raises(ShopBackendError) as err:
        make_backend(Scripted(rules)).create_refund("#1001", 1000, key="k1")
    assert err.value.code == "REFUND_REJECTED"


def test_cancel_order_waits_until_shopify_shows_it_as_cancelled():
    state = {"cancelled": False}

    def orders(_variables: dict[str, Any]) -> dict[str, Any]:
        node = {**PLACED, "cancelledAt": "2026-10-01T11:00:00Z"} if state["cancelled"] else PLACED
        return {"orders": {"nodes": [node]}}

    def cancel(_variables: dict[str, Any]) -> dict[str, Any]:
        state["cancelled"] = True
        return {"orderCancel": {"job": {"id": "j1", "done": False}, "orderCancelUserErrors": []}}

    fake = Scripted(
        [
            ("orderCancel(", cancel),
            ("customer(id", static({"customer": {"orders": {"nodes": []}}})),
            ("orders(first: $n", orders),
        ]
    )
    order = make_backend(fake).cancel_order("#1001")
    assert order.status == "cancelled"
    assert fake.sent("orderCancel(")[0]["id"] == "gid://shopify/Order/1001"


def test_a_delivered_order_cannot_be_cancelled():
    fake = Scripted(
        [
            ("orderCancel(", static({})),
            ("customer(id", static({"customer": {"orders": {"nodes": []}}})),
            ("orders(first: $n", static({"orders": {"nodes": [DELIVERED]}})),
        ]
    )
    with pytest.raises(ShopBackendError) as err:
        make_backend(fake).cancel_order("#1001")
    assert err.value.code == "ORDER_NOT_CANCELLABLE"
    assert fake.sent("orderCancel(") == []  # Shopify was never asked to cancel
