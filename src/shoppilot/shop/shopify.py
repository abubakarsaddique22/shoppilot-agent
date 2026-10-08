"""ShopifyBackend: the real store behind the ShopBackend interface (Step E).

Every method returns the same values as MockShop (see the mapping notes at the top of mockshop.py): reads (orders,
customers, shipments, products, stock), writes (refund, cancel, product draft, purchase order draft) and sales_summary.
Every failure is raised as a ShopBackendError (or another AppError). The tools turn it into an escalation, so the agent
never guesses when Shopify does not answer.

Facts that decide how this class is built:
- Login is the client-credentials grant: the token lives about 24 hours, so it is cached and renewed before it expires.
- Shopify cannot find a shipment by tracking number. We remember tracking_no -> order name when an order is read,
  and fall back to scanning the latest orders.
- Shopify has no reorder point or sales velocity. They are read from variant metafields in the namespace "shoppilot"
  (keys reorder_point and avg_daily_sales). A missing metafield gives 0, so the SKU never shows up as low stock.
- Amounts are whole PKR (the store currency must be PKR, check scripts/check_shopify.py). Datetimes are naive UTC.
- Anything that arrives from Shopify (note, tags, titles) is untrusted text. This class only copies it into the models.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from shoppilot.core.errors import (
    ConfigError,
    IdempotencyConflict,
    NotFound,
    OrderNotFound,
    ShopBackendError,
    ValidationFailed,
)
from shoppilot.core.logging import get_logger
from shoppilot.db.models import PurchaseDraftRow
from shoppilot.shop.base import (
    Customer,
    InventoryLevel,
    LowStockItem,
    Order,
    OrderItem,
    Product,
    ProductDraft,
    PurchaseOrderDraft,
    Refund,
    SalesSummary,
    Shipment,
    ShipmentEvent,
)

log = get_logger(__name__)

DEFAULT_DELIVERY_DAYS = 5  # estimate for an order that has no fulfillment yet (same as MockShop)
TIMEOUT_S = 15.0
DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]*\.myshopify\.com")
ORDER_REF_RE = re.compile(r"#?[A-Za-z0-9-]{1,30}")
TRACKING_SCAN_LIMIT = 50

# Fulfillment.displayStatus -> the three values of Shipment.status
DELAYED_STATUSES = {"DELAYED", "NOT_DELIVERED", "ATTEMPTED_DELIVERY", "FAILURE"}

ORDER_FIELDS = """
  id name email createdAt processedAt cancelledAt note
  displayFinancialStatus paymentGatewayNames
  totalPriceSet { shopMoney { amount } }
  customer { id tags }
  lineItems(first: 50) {
    nodes { sku title quantity originalUnitPriceSet { shopMoney { amount } } product { tags } }
  }
  fulfillments(first: 5) {
    id status displayStatus deliveredAt estimatedDeliveryAt updatedAt
    trackingInfo(first: 1) { number company }
    events(first: 20) { nodes { status happenedAt city province message } }
  }
  transactions(first: 50) { id kind status gateway amountSet { shopMoney { amount } } createdAt }
"""

Q_ORDERS = "query($q: String!, $n: Int!) { orders(first: $n, query: $q, sortKey: CREATED_AT, reverse: true) { nodes {" + ORDER_FIELDS + "} } }"  # noqa: E501

# The demo simulator only needs the name and the e-mail of the seeded test orders (tag shoppilot-test-<kind>).
Q_TEST_ORDERS = "query($q: String!, $n: Int!) { orders(first: $n, query: $q, sortKey: CREATED_AT) { nodes { name email cancelledAt displayFinancialStatus } } }"  # noqa: E501

Q_CUSTOMER_REFUNDS = """
query($id: ID!) {
  customer(id: $id) {
    orders(first: 50, sortKey: PROCESSED_AT, reverse: true) { nodes { refunds(first: 10) { createdAt } } }
  }
}
"""

CUSTOMER_FIELDS = """
  id firstName lastName tags
  defaultEmailAddress { emailAddress }
  defaultPhoneNumber { phoneNumber }
  defaultAddress { city }
"""
Q_CUSTOMER_BY_ID = "query($id: ID!) { customer(id: $id) {" + CUSTOMER_FIELDS + "} }"
Q_CUSTOMER_BY_EMAIL = "query($q: String!) { customers(first: 1, query: $q) { nodes {" + CUSTOMER_FIELDS + "} } }"

Q_VARIANTS = """
query($q: String!) {
  productVariants(first: 5, query: $q) {
    nodes {
      sku price
      product { title productType vendor tags }
      inventoryItem { inventoryLevels(first: 10) { nodes { quantities(names: ["available"]) { name quantity } } } }
      reorderPoint: metafield(namespace: "shoppilot", key: "reorder_point") { value }
      avgDailySales: metafield(namespace: "shoppilot", key: "avg_daily_sales") { value }
    }
  }
}
"""


Q_ORDER_REFUNDS = """
query($id: ID!) {
  order(id: $id) {
    refunds(first: 50) {
      id createdAt note totalRefundedSet { shopMoney { amount } } transactions(first: 5) { nodes { status } }
    }
  }
}
"""

# Since API 2026-04 refundCreate REQUIRES the @idempotent key (Shopify docs). A retry with the same key does nothing twice.
M_REFUND_CREATE = """
mutation($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) {
    refund {
      id createdAt note totalRefundedSet { shopMoney { amount } } transactions(first: 5) { nodes { status } }
    }
    userErrors { field message }
  }
}
"""

M_ORDER_CANCEL = """
mutation($id: ID!) {
  orderCancel(orderId: $id, reason: OTHER, refund: false, restock: true, notifyCustomer: false) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
"""

KEY_MARKER = "[shoppilot:"  # written into the refund note, so a repeated call finds its earlier refund

# A product made with status DRAFT is not visible in the shop. The agent can never publish it.
M_PRODUCT_CREATE = """
mutation($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id title status createdAt }
    userErrors { field message }
  }
}
"""
CANCEL_POLLS = 5  # orderCancel runs as a background job: we look again a few times

# --- sales_summary: small queries (only the fields the report needs), read page by page ---
MAX_PAGES = 10  # at most 500 rows per question; a bigger store needs a smarter way later

Q_DAY_ORDERS = """
query($q: String!, $after: String) {
  orders(first: 50, query: $q, after: $after) {
    nodes {
      name createdAt cancelledAt totalPriceSet { shopMoney { amount } }
      transactions(first: 10) { kind status createdAt amountSet { shopMoney { amount } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_SHIPPED_ORDERS = """
query($q: String!, $after: String) {
  orders(first: 50, query: $q, after: $after, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name cancelledAt displayFinancialStatus
      fulfillments(first: 5) { status displayStatus deliveredAt estimatedDeliveryAt }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_ALL_VARIANTS = """
query($after: String) {
  productVariants(first: 50, after: $after) {
    nodes {
      sku
      inventoryItem { inventoryLevels(first: 10) { nodes { quantities(names: ["available"]) { name quantity } } } }
      reorderPoint: metafield(namespace: "shoppilot", key: "reorder_point") { value }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# ------------------------------------------------------------------ small helpers
def _parse_dt(value: str | None) -> datetime | None:
    """Shopify sends ISO times with Z. We use naive UTC everywhere."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _money(node: dict[str, Any] | None) -> int:
    """{"shopMoney": {"amount": "5400.0"}} -> 5400 (whole PKR)."""
    amount = ((node or {}).get("shopMoney") or {}).get("amount") or "0"
    return int(round(Decimal(str(amount))))


def _numeric_id(gid: str | None) -> str:
    return gid.rsplit("/", 1)[-1] if gid else ""


def _has_tag(tags: list[str] | None, tag: str) -> bool:
    return tag in [t.strip().lower() for t in (tags or [])]


def _is_cod(gateways: list[str] | None) -> bool:
    text = " ".join(gateways or []).lower()
    return "cash on delivery" in text or "(cod)" in text


def _quoted(value: str) -> str:
    """A value for the Shopify search syntax, so the text cannot add its own filters."""
    return '"' + value.replace("\\", " ").replace('"', " ").strip() + '"'


def _order_name(ref: str) -> str:
    """'88731' or '#88731' -> '#88731'. Anything that does not look like an order number is 'not found'."""
    ref = str(ref).strip()
    if not ORDER_REF_RE.fullmatch(ref):
        raise OrderNotFound(f"order {ref[:40]} not found", details={"order_id": ref[:40]})
    return ref if ref.startswith("#") else "#" + ref


class ShopifyBackend:
    def __init__(
        self,
        domain: str,
        client_id: str,
        client_secret: str,
        api_version: str = "2026-07",
        session_factory: Any = None,
        now: Callable[[], datetime] | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        domain = (domain or "").strip().lower()
        if not DOMAIN_RE.fullmatch(domain):
            # The secret is sent to this host, so a typo or a wrong host must stop here.
            raise ConfigError("SHOP_SHOPIFY_STORE_DOMAIN must look like my-store.myshopify.com")
        if not client_id.strip() or not client_secret.strip():
            raise ConfigError("SHOP_SHOPIFY_CLIENT_ID and SHOP_SHOPIFY_CLIENT_SECRET are required")
        self._domain = domain
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._graphql_url = f"https://{domain}/admin/api/{api_version}/graphql.json"
        self._token_url = f"https://{domain}/admin/oauth/access_token"
        self._sf = session_factory  # for purchase order drafts (own table)
        self._now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self._http = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._access_token = ""
        self._expires_at = 0.0
        self._tracking_index: dict[str, str] = {}  # tracking_no -> order name

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ transport
    def _token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            try:
                resp = self._http.post(
                    self._token_url,
                    json={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "client_credentials",
                    },
                    timeout=TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                raise ShopBackendError("Shopify login failed: no answer", code="SHOP_AUTH_FAILED") from exc
            if resp.status_code != 200:
                raise ShopBackendError(
                    f"Shopify login failed (HTTP {resp.status_code})", code="SHOP_AUTH_FAILED"
                )  # the secret is never part of this message
            body = resp.json()
            self._access_token = str(body["access_token"])
            lifetime = int(body.get("expires_in", 3600))
            self._expires_at = time.monotonic() + max(60, lifetime - 300)  # renew 5 minutes early
            return self._access_token

    def _drop_token(self) -> None:
        with self._lock:
            self._access_token = ""
            self._expires_at = 0.0

    def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GraphQL call. Renews the token once on 401, waits and retries on throttling, maps every failure to
        ShopBackendError (the tools then escalate or say 'status unknown' instead of guessing)."""
        renewed = False
        for attempt in range(3):
            try:
                resp = self._http.post(
                    self._graphql_url,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": self._token()},
                    timeout=TIMEOUT_S,
                )
            except httpx.TimeoutException as exc:
                raise ShopBackendError("Shopify did not answer in time", code="SHOP_TIMEOUT") from exc
            except httpx.HTTPError as exc:
                raise ShopBackendError("Shopify cannot be reached", code="SHOP_UNREACHABLE") from exc

            if resp.status_code == 401 and not renewed:
                renewed = True
                self._drop_token()
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < 2:
                    self._sleep(float(resp.headers.get("Retry-After", 1 + attempt)))
                    continue
                raise ShopBackendError(f"Shopify is busy (HTTP {resp.status_code})", code="SHOP_THROTTLED")
            if resp.status_code != 200:
                raise ShopBackendError(f"Shopify answered HTTP {resp.status_code}", code="SHOP_HTTP_ERROR")

            payload = resp.json()
            errors = payload.get("errors")
            if errors:
                items = errors if isinstance(errors, list) else [errors]
                if attempt < 2 and any(
                    isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "THROTTLED" for e in items
                ):
                    self._sleep(1 + attempt)
                    continue
                messages = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in items]
                raise ShopBackendError(
                    "Shopify rejected the query", code="SHOP_GRAPHQL_ERROR", details={"errors": messages[:3]}
                )
            return payload.get("data") or {}
        raise ShopBackendError("Shopify is busy, try again later", code="SHOP_THROTTLED")

    # ------------------------------------------------------------------ orders
    def _order_node(self, order_id: str) -> dict[str, Any]:
        name = _order_name(order_id)
        data = self._gql(Q_ORDERS, {"q": f"name:{_quoted(name)}", "n": 3})
        for node in data["orders"]["nodes"]:
            if node["name"] == name:  # the search can be loose, so compare the name ourselves
                return node
        raise OrderNotFound(f"order {name} not found", details={"order_id": name})

    def get_order(self, order_id: str) -> Order:
        return self._order(self._order_node(order_id), {})

    def find_orders(self, email: str, limit: int = 5) -> list[Order]:
        email = email.strip().lower()
        if not email:
            return []
        data = self._gql(Q_ORDERS, {"q": f"email:{_quoted(email)}", "n": max(1, min(limit, 20))})
        cache: dict[str, int] = {}  # one refund-history query per customer, not one per order
        orders = [self._order(n, cache) for n in data["orders"]["nodes"]]
        return [o for o in orders if o.customer_email.lower() == email]  # exact match only

    def find_test_orders(self, kind: str, limit: int = 10) -> list[tuple[str, str]]:
        """(order name, customer email) of the seeded test orders of one kind, oldest first. Used only by the demo
        simulator. Cancelled and (partly) refunded orders are skipped, because a demo needs a fresh order, and so are
        orders whose e-mail is not visible."""
        data = self._gql(Q_TEST_ORDERS, {"q": f"tag:{_quoted('shoppilot-test-' + kind)}", "n": limit})
        used_up = ("REFUNDED", "PARTIALLY_REFUNDED")
        return [
            (n["name"], n["email"])
            for n in data["orders"]["nodes"]
            if n.get("email") and not n.get("cancelledAt") and n.get("displayFinancialStatus") not in used_up
        ]

    def get_customer(self, ref: str) -> Customer:
        ref = ref.strip()
        if ref.isdigit():
            node = self._gql(Q_CUSTOMER_BY_ID, {"id": f"gid://shopify/Customer/{ref}"}).get("customer")
        else:
            nodes = self._gql(Q_CUSTOMER_BY_EMAIL, {"q": f"email:{_quoted(ref.lower())}"})["customers"]["nodes"]
            node = nodes[0] if nodes else None
        if node is None:
            raise NotFound(f"customer {ref} not found", code="CUSTOMER_NOT_FOUND")
        return Customer(
            id=_numeric_id(node["id"]),
            name=f"{node.get('firstName') or ''} {node.get('lastName') or ''}".strip(),
            email=((node.get("defaultEmailAddress") or {}).get("emailAddress")) or "",
            phone=((node.get("defaultPhoneNumber") or {}).get("phoneNumber")) or "",
            city=((node.get("defaultAddress") or {}).get("city")) or "",
            flagged=_has_tag(node.get("tags"), "flagged"),
        )

    def track_shipment(self, tracking_no: str) -> Shipment:
        wanted = tracking_no.strip()
        name = self._tracking_index.get(wanted)
        nodes: list[dict[str, Any]]
        if name:
            nodes = self._gql(Q_ORDERS, {"q": f"name:{_quoted(name)}", "n": 3})["orders"]["nodes"]
        else:
            nodes = self._gql(Q_ORDERS, {"q": "", "n": TRACKING_SCAN_LIMIT})["orders"]["nodes"]
        for node in nodes:
            for f in node.get("fulfillments") or []:
                info = (f.get("trackingInfo") or [{}])[0]
                if info.get("number") == wanted:
                    self._tracking_index[wanted] = node["name"]
                    return self._shipment(node["name"], f, info)
        raise NotFound(f"shipment {wanted} not found", code="SHIPMENT_NOT_FOUND")

    # ------------------------------------------------------------------ products and stock
    def _variant(self, sku: str) -> dict[str, Any]:
        sku = sku.strip()
        if not sku:
            raise NotFound("product not found", code="PRODUCT_NOT_FOUND")
        nodes = self._gql(Q_VARIANTS, {"q": f"sku:{_quoted(sku)}"})["productVariants"]["nodes"]
        for node in nodes:
            if (node.get("sku") or "").lower() == sku.lower():  # exact SKU only
                return node
        raise NotFound(f"product {sku} not found", code="PRODUCT_NOT_FOUND")

    def get_product(self, sku: str) -> Product:
        v = self._variant(sku)
        p = v["product"]
        return Product(
            sku=v["sku"],
            title=p["title"],
            category=p.get("productType") or "",
            price_pkr=int(round(Decimal(str(v.get("price") or "0")))),
            refundable=not _has_tag(p.get("tags"), "non-refundable"),
            supplier=p.get("vendor") or "",
        )

    def get_inventory(self, sku: str) -> InventoryLevel:
        v = self._variant(sku)
        levels = ((v.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("nodes") or []
        on_hand = sum(
            int(q["quantity"]) for level in levels for q in level.get("quantities") or [] if q.get("name") == "available"
        )
        reorder = (v.get("reorderPoint") or {}).get("value")
        velocity = (v.get("avgDailySales") or {}).get("value")
        return InventoryLevel(
            sku=v["sku"],
            on_hand=on_hand,
            reorder_point=int(float(reorder)) if reorder else 0,
            avg_daily_sales=float(velocity) if velocity else 0.0,
        )

    # ------------------------------------------------------------------ writes: money
    def create_refund(self, order_id: str, amount_pkr: int, key: str, reason: str = "") -> Refund:
        """Idempotent, like MockShop: the same key returns the earlier refund and never refunds twice.

        Two guards: (1) the key is written into the refund note and looked up first, so a repeat is found even after the
        balance has changed; (2) Shopify's own @idempotent key (a UUID made from our key) covers a retry that raced.
        Safety net as in MockShop: never more than the order total minus earlier refunds.
        """
        if amount_pkr <= 0:
            raise ShopBackendError("refund amount must be positive", code="INVALID_AMOUNT")
        key = key.strip()
        if not key:
            raise ShopBackendError("a refund needs an idempotency key", code="INVALID_KEY")

        node = self._order_node(order_id)
        order = self._order(node, {})
        gid = node["id"]

        existing = self._gql(Q_ORDER_REFUNDS, {"id": gid}).get("order") or {}
        for r in existing.get("refunds") or []:
            if f"{KEY_MARKER}{key}]" in (r.get("note") or ""):
                earlier = self._refund(r, key, order.id)
                if earlier.amount_pkr != amount_pkr:
                    raise IdempotencyConflict(
                        "this idempotency key was already used for a different refund", details={"key": key}
                    )
                return earlier

        balance = order.amount_paid - order.refunded_total
        if amount_pkr > balance:
            raise ShopBackendError(
                f"refund {amount_pkr} is above the refundable balance {balance}",
                code="REFUND_EXCEEDS_BALANCE",
                details={"order_id": order.id},
            )

        paid = [
            t for t in node.get("transactions") or [] if t.get("kind") in ("SALE", "CAPTURE") and t.get("status") == "SUCCESS"
        ]
        if not paid:
            raise ShopBackendError("the order has no paid transaction to refund", code="NO_PAYMENT_TRANSACTION")
        parent = paid[-1]

        clean_reason = " ".join(reason.split())[:200]  # one line, short: the note is read by staff
        note = f"{clean_reason} {KEY_MARKER}{key}]".strip()
        data = self._gql(
            M_REFUND_CREATE,
            {
                "input": {
                    "orderId": gid,
                    "note": note,
                    "transactions": [
                        {
                            "orderId": gid,
                            "parentId": parent["id"],
                            "kind": "REFUND",
                            "gateway": parent["gateway"],
                            "amount": str(amount_pkr),
                        }
                    ],
                },
                "key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"shoppilot:{key}")),
            },
        )
        result = data.get("refundCreate") or {}
        if result.get("userErrors"):
            messages = [str(e.get("message", e)) for e in result["userErrors"]]
            raise ShopBackendError(
                "Shopify refused the refund", code="REFUND_REJECTED", details={"errors": messages[:3]}
            )
        if not result.get("refund"):
            raise ShopBackendError("Shopify returned no refund", code="REFUND_REJECTED")
        log.info("refund created", extra={"order": order.id, "amount": amount_pkr})
        return self._refund(result["refund"], key, order.id)

    def cancel_order(self, order_id: str) -> Order:
        """Only an order that is still 'placed' can be cancelled (same rule as MockShop). Shopify cancels in the
        background, so we look at the order a few times until it shows as cancelled. No refund, no customer email."""
        node = self._order_node(order_id)
        current = self._order(node, {})
        if current.status != "placed":
            raise ShopBackendError(
                f"order is {current.status} and cannot be cancelled", code="ORDER_NOT_CANCELLABLE"
            )
        result = self._gql(M_ORDER_CANCEL, {"id": node["id"]}).get("orderCancel") or {}
        if result.get("orderCancelUserErrors"):
            messages = [str(e.get("message", e)) for e in result["orderCancelUserErrors"]]
            raise ShopBackendError(
                "Shopify refused the cancellation", code="ORDER_NOT_CANCELLABLE", details={"errors": messages[:3]}
            )
        for attempt in range(CANCEL_POLLS):
            after = self.get_order(current.id)
            if after.status == "cancelled":
                return after
            self._sleep(1 + attempt)
        raise ShopBackendError("the cancellation is still running, check again later", code="CANCEL_PENDING")

    def _refund(self, r: dict[str, Any], key: str, order_name: str) -> Refund:
        txs = (r.get("transactions") or {}).get("nodes") or []
        note = r.get("note") or ""
        return Refund(
            id=int(_numeric_id(r["id"]) or 0),
            order_id=order_name,
            amount_pkr=_money(r.get("totalRefundedSet")),
            reason=note.split(KEY_MARKER)[0].strip(),
            status="pending" if any(t.get("status") == "PENDING" for t in txs) else "issued",
            idempotency_key=key,
            created_at=_parse_dt(r.get("createdAt")) or self._now(),
        )

    def create_product_draft(self, fields: dict[str, Any]) -> ProductDraft:
        """Makes a product with status DRAFT (same as MockShop). A repeated call is stopped earlier by the tool
        (idempotency_key), so this method just creates one draft. The text is already escaped by tools/listings.py."""
        title = str(fields.get("title", "")).strip()
        if not title:
            raise ValidationFailed("a product draft needs a title")
        tags = fields.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        product_input = {
            "title": title,
            "descriptionHtml": str(fields.get("body_html") or fields.get("description") or ""),
            "vendor": str(fields.get("vendor", "")),
            "productType": str(fields.get("product_type", "")),
            "tags": [str(t) for t in tags],
            "status": "DRAFT",  # fixed here, never taken from `fields`
        }
        result = self._gql(M_PRODUCT_CREATE, {"product": product_input}).get("productCreate") or {}
        if result.get("userErrors"):
            messages = [str(e.get("message", e)) for e in result["userErrors"]]
            raise ShopBackendError(
                "Shopify refused the product draft", code="PRODUCT_REJECTED", details={"errors": messages[:3]}
            )
        product = result.get("product")
        if not product:
            raise ShopBackendError("Shopify returned no product", code="PRODUCT_REJECTED")
        log.info("product draft created", extra={"title": title})
        return ProductDraft(
            id=int(_numeric_id(product["id"]) or 0),
            title=product["title"],
            status=str(product.get("status") or "DRAFT").lower(),
            fields=fields,
            created_at=_parse_dt(product.get("createdAt")) or self._now(),
        )

    def create_purchase_order_draft(self, sku: str, qty: int, supplier: str) -> PurchaseOrderDraft:
        """Shopify has no purchase orders, so the draft is a row in our own table (purchase_drafts). The SKU is checked
        against the real store first (NotFound for an unknown SKU, same as MockShop). A person reviews it later."""
        supplier = supplier.strip()
        if qty <= 0 or not supplier:
            raise ValidationFailed("a purchase order draft needs a positive quantity and a supplier")
        if self._sf is None:
            raise ConfigError("purchase order drafts need a database session")
        real_sku = self._variant(sku)["sku"]
        with self._sf() as s:
            row = PurchaseDraftRow(sku=real_sku, qty=qty, supplier=supplier, status="draft", created_at=self._now())
            s.add(row)
            s.commit()
            return PurchaseOrderDraft(
                id=row.id, sku=row.sku, qty=row.qty, supplier=row.supplier, status=row.status, created_at=row.created_at
            )

    def _paged(self, query: str, root: str, variables: dict[str, Any]) -> list[dict[str, Any]]:
        """Reads a GraphQL list page by page (50 rows each). Stops after MAX_PAGES and writes a warning."""
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            block = self._gql(query, {**variables, "after": cursor})[root]
            nodes.extend(block["nodes"])
            if not block["pageInfo"]["hasNextPage"]:
                return nodes
            cursor = block["pageInfo"]["endCursor"]
        log.warning("shopify list was cut after %s rows", len(nodes), extra={"root": root})
        return nodes

    def sales_summary(self, day: date) -> SalesSummary:
        """The numbers of one day (same meaning as MockShop). Orders and refunds are of that day (UTC).
        Late orders and low stock are as of now. Low stock needs the metafield reorder_point (missing = 0 = never low)."""
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        # 1) orders placed that day (cancelled ones are not counted)
        created = self._paged(
            Q_DAY_ORDERS, "orders", {"q": f"created_at:>={start.strftime(fmt)} created_at:<{end.strftime(fmt)}"}
        )
        orders_count = 0
        sales = 0
        for node in created:
            placed = _parse_dt(node["createdAt"])
            if node.get("cancelledAt") or placed is None or not (start <= placed < end):
                continue  # the date is checked again here, so a loose search cannot change the numbers
            orders_count += 1
            sales += _money(node.get("totalPriceSet"))

        # 2) refunds made that day: any order changed since the start of the day, then look at its refund transactions
        touched = self._paged(Q_DAY_ORDERS, "orders", {"q": f"updated_at:>={start.strftime(fmt)}"})
        refunds_count = 0
        refunds_pkr = 0
        for node in touched:
            for t in node.get("transactions") or []:
                made = _parse_dt(t.get("createdAt"))
                if t.get("kind") == "REFUND" and t.get("status") == "SUCCESS" and made and start <= made < end:
                    refunds_count += 1
                    refunds_pkr += _money(t.get("amountSet"))

        # 3) late orders: shipped, not delivered, estimated date already passed (the newest orders only)
        now = self._now()
        late_orders = 0
        for node in self._paged(Q_SHIPPED_ORDERS, "orders", {"q": "fulfillment_status:shipped"}):
            if node.get("cancelledAt") or node.get("displayFinancialStatus") == "REFUNDED":
                continue
            for f in node.get("fulfillments") or []:
                estimated = _parse_dt(f.get("estimatedDeliveryAt"))
                undelivered = f.get("deliveredAt") is None and f.get("displayStatus") != "DELIVERED"
                if f.get("status") == "SUCCESS" and undelivered and estimated is not None and estimated < now:
                    late_orders += 1
                    break  # one order is counted once

        # 4) low stock: on hand at or below the reorder point (a variant without reorder_point is skipped)
        low: list[LowStockItem] = []
        for node in self._paged(Q_ALL_VARIANTS, "productVariants", {}):
            sku = (node.get("sku") or "").strip()
            raw = (node.get("reorderPoint") or {}).get("value")
            reorder = int(float(raw)) if raw else 0
            if not sku or reorder <= 0:
                continue
            levels = ((node.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("nodes") or []
            on_hand = sum(
                int(q["quantity"]) for lv in levels for q in lv.get("quantities") or [] if q.get("name") == "available"
            )
            if on_hand <= reorder:
                low.append(LowStockItem(sku=sku, on_hand=on_hand, reorder_point=reorder))
        low.sort(key=lambda item: item.sku)

        return SalesSummary(
            day=day,
            orders_count=orders_count,
            sales_pkr=sales,
            refunds_count=refunds_count,
            refunds_pkr=refunds_pkr,
            late_orders=late_orders,
            low_stock=low,
        )

    # ------------------------------------------------------------------ mapping
    def _refunds_last_90d(self, customer_gid: str, cache: dict[str, int]) -> int:
        """Refunds of this customer in the last 90 days. A failure is NOT hidden: with an unknown history the policy
        engine could wrongly allow an automatic refund, so the error goes up and the ticket escalates."""
        if customer_gid not in cache:
            data = self._gql(Q_CUSTOMER_REFUNDS, {"id": customer_gid}).get("customer") or {}
            since = self._now() - timedelta(days=90)
            count = 0
            for order in (data.get("orders") or {}).get("nodes") or []:
                for refund in order.get("refunds") or []:
                    created = _parse_dt(refund.get("createdAt"))
                    if created is not None and created >= since:
                        count += 1
            cache[customer_gid] = count
        return cache[customer_gid]

    def _shipment(self, order_name: str, f: dict[str, Any], info: dict[str, Any]) -> Shipment:
        display = f.get("displayStatus") or ""
        status = "delivered" if display == "DELIVERED" else "delayed" if display in DELAYED_STATUSES else "in_transit"
        events = sorted(
            (
                ShipmentEvent(
                    ts=_parse_dt(e["happenedAt"]) or self._now(),
                    status=(e.get("status") or "").lower(),
                    location=", ".join(x for x in (e.get("city"), e.get("province")) if x),
                )
                for e in (f.get("events") or {}).get("nodes") or []
            ),
            key=lambda e: e.ts,
        )
        last = events[-1].ts if events else (_parse_dt(f.get("updatedAt")) or self._now())
        return Shipment(
            tracking_no=info["number"],
            order_id=order_name,
            courier=info.get("company") or "",
            status=status,
            last_update=last,
            events=events,
        )

    def _order(self, node: dict[str, Any], refund_cache: dict[str, int]) -> Order:
        now = self._now()
        name = node["name"]
        customer = node.get("customer") or {}
        customer_gid = customer.get("id") or ""

        done = [f for f in node.get("fulfillments") or [] if f.get("status") == "SUCCESS"]
        fulfillment = done[-1] if done else None
        info = ((fulfillment or {}).get("trackingInfo") or [{}])[0] if fulfillment else {}
        for f in done:  # remember every tracking number, so track_shipment finds the order fast
            number = ((f.get("trackingInfo") or [{}])[0]).get("number")
            if number:
                self._tracking_index[number] = name

        placed_at = _parse_dt(node.get("processedAt")) or _parse_dt(node["createdAt"]) or now
        delivered_at = _parse_dt((fulfillment or {}).get("deliveredAt"))
        if delivered_at is None and fulfillment and fulfillment.get("displayStatus") == "DELIVERED":
            delivered_at = _parse_dt(fulfillment.get("updatedAt"))
        estimated = _parse_dt((fulfillment or {}).get("estimatedDeliveryAt")) or placed_at + timedelta(
            days=DEFAULT_DELIVERY_DAYS
        )

        if node.get("cancelledAt"):
            status = "cancelled"
        elif node.get("displayFinancialStatus") == "REFUNDED":
            status = "refunded"
        elif delivered_at is not None:
            status = "delivered"
        elif fulfillment is not None:
            status = "shipped"
        else:
            status = "placed"

        if delivered_at is not None:
            is_overdue = False
            days_late = max(0, (delivered_at - estimated).days)
        else:
            is_overdue = status not in ("cancelled", "refunded") and now > estimated
            days_late = max(0, (now - estimated).days) if is_overdue else 0

        refund_txs = [t for t in node.get("transactions") or [] if t.get("kind") == "REFUND"]
        items = [
            OrderItem(
                sku=i.get("sku") or "",
                title=i["title"],
                qty=int(i["quantity"]),
                unit_price_pkr=_money(i.get("originalUnitPriceSet")),
            )
            for i in (node.get("lineItems") or {}).get("nodes") or []
        ]
        return Order(
            id=name,
            customer_id=_numeric_id(customer_gid),
            customer_email=node.get("email") or "",  # empty when protected customer data access is missing
            status=status,
            payment_method="cod" if _is_cod(node.get("paymentGatewayNames")) else "prepaid",
            amount_paid=_money(node.get("totalPriceSet")),
            refunded_total=sum(_money(t.get("amountSet")) for t in refund_txs if t.get("status") == "SUCCESS"),
            placed_at=placed_at,
            estimated_delivery=estimated,
            delivered_at=delivered_at,
            tracking_no=info.get("number") or None,
            items=items,
            note=node.get("note") or "",
            days_late=days_late,
            is_overdue=is_overdue,
            customer_flagged=_has_tag(customer.get("tags"), "flagged"),
            refunds_last_90d=self._refunds_last_90d(customer_gid, refund_cache) if customer_gid else 0,
            has_open_refund=any(t.get("status") == "PENDING" for t in refund_txs),
            non_refundable=any(
                _has_tag((i.get("product") or {}).get("tags"), "non-refundable")
                for i in (node.get("lineItems") or {}).get("nodes") or []
            ),
        )
