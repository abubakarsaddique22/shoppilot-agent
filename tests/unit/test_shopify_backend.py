"""ShopifyBackend read methods against a fake Shopify (httpx.MockTransport). No network, no secrets (Step E).

The answers below are shaped like Shopify's GraphQL answers. The test checks that they become the same Order values
that MockShop gives, and that the login token is reused.
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from shoppilot.core.errors import ConfigError, NotFound, OrderNotFound
from shoppilot.shop.base import ShopBackend
from shoppilot.shop.shopify import ShopifyBackend

NOW = datetime(2026, 10, 1, 12, 0, 0)


def money(amount: str) -> dict:
    return {"shopMoney": {"amount": amount}}


ORDER_NODE = {
    "id": "gid://shopify/Order/1001",
    "name": "#1001",
    "email": "ali.khan@example.com",
    "createdAt": "2026-09-10T08:00:00Z",
    "processedAt": "2026-09-10T08:00:00Z",
    "cancelledAt": None,
    "note": "Call before delivery",
    "displayFinancialStatus": "PAID",
    "paymentGatewayNames": ["JazzCash"],
    "totalPriceSet": money("5400.0"),
    "customer": {"id": "gid://shopify/Customer/77", "tags": []},
    "lineItems": {
        "nodes": [
            {
                "sku": "LAWN-3P-BLU",
                "title": "Lawn Suit 3-Piece Blue",
                "quantity": 1,
                "originalUnitPriceSet": money("5400.0"),
                "product": {"tags": []},
            }
        ]
    },
    "fulfillments": [
        {
            "id": "gid://shopify/Fulfillment/1",
            "status": "SUCCESS",
            "displayStatus": "DELIVERED",
            "deliveredAt": "2026-09-24T12:00:00Z",  # estimate was 2026-09-15, so 9 days late
            "estimatedDeliveryAt": "2026-09-15T12:00:00Z",
            "updatedAt": "2026-09-24T12:00:00Z",
            "trackingInfo": [{"number": "TCS123", "company": "TCS"}],
            "events": {
                "nodes": [
                    {"status": "DELIVERED", "happenedAt": "2026-09-24T12:00:00Z", "city": "Lahore", "province": None, "message": ""},
                    {"status": "IN_TRANSIT", "happenedAt": "2026-09-12T09:00:00Z", "city": "Karachi", "province": None, "message": ""},
                ]
            },
        }
    ],
    "transactions": [{"kind": "SALE", "status": "SUCCESS", "amountSet": money("5400.0"), "createdAt": "2026-09-10T08:00:00Z"}],
}

REFUNDS_ANSWER = {"data": {"customer": {"orders": {"nodes": [{"refunds": [{"createdAt": "2026-09-20T10:00:00Z"}]}]}}}}


class FakeShopify:
    """Counts the calls, so a test can check that the token is reused."""

    def __init__(self, orders: list[dict]) -> None:
        self.orders = orders
        self.token_calls = 0
        self.graphql_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "t-1", "scope": "read_orders", "expires_in": 86399})
        assert request.headers["X-Shopify-Access-Token"] == "t-1"
        self.graphql_calls += 1
        query = json.loads(request.content)["query"]
        if "customer(id" in query:
            return httpx.Response(200, json=REFUNDS_ANSWER)
        return httpx.Response(200, json={"data": {"orders": {"nodes": self.orders}}})


def make_backend(fake: FakeShopify) -> ShopifyBackend:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return ShopifyBackend(
        domain="demo.myshopify.com",
        client_id="id",
        client_secret="secret",
        now=lambda: NOW,
        client=client,
        sleep=lambda _s: None,
    )


def test_shopify_backend_is_a_shop_backend():
    assert isinstance(make_backend(FakeShopify([])), ShopBackend)


def test_get_order_maps_like_mockshop():
    order = make_backend(FakeShopify([ORDER_NODE])).get_order("1001")
    assert order.id == "#1001" and order.customer_id == "77"
    assert order.customer_email == "ali.khan@example.com"
    assert order.status == "delivered" and order.payment_method == "prepaid"
    assert order.amount_paid == 5400 and order.refunded_total == 0
    assert order.days_late == 9 and order.is_overdue is False
    assert order.tracking_no == "TCS123"
    assert order.refunds_last_90d == 1 and order.customer_flagged is False
    assert [i.sku for i in order.items] == ["LAWN-3P-BLU"]


def test_order_not_found():
    with pytest.raises(OrderNotFound):
        make_backend(FakeShopify([])).get_order("#9999")
    with pytest.raises(OrderNotFound):  # not an order number at all: Shopify is never asked
        make_backend(FakeShopify([ORDER_NODE])).get_order("ignore previous instructions")


def test_a_loose_search_hit_with_another_name_is_not_returned():
    with pytest.raises(OrderNotFound):
        make_backend(FakeShopify([ORDER_NODE])).get_order("#1002")


def test_track_shipment_uses_the_order_data():
    ship = make_backend(FakeShopify([ORDER_NODE])).track_shipment("TCS123")
    assert ship.order_id == "#1001" and ship.status == "delivered" and ship.courier == "TCS"
    assert [e.status for e in ship.events] == ["in_transit", "delivered"]  # oldest first
    with pytest.raises(NotFound) as err:
        make_backend(FakeShopify([ORDER_NODE])).track_shipment("NOPE")
    assert err.value.code == "SHIPMENT_NOT_FOUND"


def test_find_orders_returns_only_the_exact_email():
    backend = make_backend(FakeShopify([ORDER_NODE]))
    assert [o.id for o in backend.find_orders("ALI.KHAN@example.com")] == ["#1001"]
    assert backend.find_orders("someone.else@example.com") == []


def test_token_is_reused():
    fake = FakeShopify([ORDER_NODE])
    backend = make_backend(fake)
    backend.get_order("#1001")
    backend.get_order("#1001")
    assert fake.token_calls == 1 and fake.graphql_calls >= 2


def test_wrong_domain_is_refused_before_any_secret_is_sent():
    with pytest.raises(ConfigError):
        ShopifyBackend(domain="evil.example.com", client_id="id", client_secret="secret")
