"""Make 4 test orders in the Shopify store (Step E). Safe to run again.

    uv run python scripts/seed_shopify_orders.py            # dry run: shows the plan, writes nothing
    uv run python scripts/seed_shopify_orders.py --apply    # really creates the orders

The four orders cover what the order checks and the real refund/cancel tests need:
  prepaid     paid, not shipped          -> get_order, find_orders, the real refund test
  cod         Cash on Delivery, unpaid   -> payment_method = "cod"
  fulfilled   paid, shipped with a tracking number -> track_shipment
  placed      paid, not shipped          -> the real cancel test (do NOT use it for the refund test)
  late_auto   paid, not shipped, placed 12 days ago -> a late order whose refund is in the AUTO tier (2400 PKR)
  late_manager paid, not shipped, placed 12 days ago -> a late order whose refund needs a MANAGER (4800 PKR)

The two late orders are for the full support ticket (item 8): a new order is never "late" yet, so their date is moved
back with processedAt. A refund needs an order that is delivered or more than 5 days late.

Every order gets the tag "shoppilot-test" (and "shoppilot-test-<kind>"), so you can find and delete them in the
Shopify admin later. An order whose kind already exists is skipped. Stock is NOT reduced (inventoryBehaviour BYPASS),
so the low-stock numbers of the 13 seed products stay the same. No e-mail goes to anybody.

Needs these scopes: write_orders, read_customers, write_merchant_managed_fulfillment_orders.
The customers must be readable (protected customer data access).
"""
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError
from shoppilot.shop.shopify import ShopifyBackend

TAG = "shoppilot-test"
COD_GATEWAY = "Cash on Delivery (COD)"
TRACKING_NO = "TRK-TEST-001"
COURIER = "TCS"

# Small prices on purpose: the real refund test should move only a little money.
PLAN: list[dict[str, Any]] = [
    {"kind": "prepaid", "customer": "karine.ruby@example.com", "items": [("CASE-PRM-01", 2)], "payment": "paid"},
    {"kind": "cod", "customer": "russel.winfield@example.com", "items": [("WALLET-LTH-BRN", 1)], "payment": "cod"},
    {"kind": "fulfilled", "customer": "ayumu.hirano@example.com", "items": [("LAMP-LED-01", 1)], "payment": "paid"},
    {"kind": "placed", "customer": "karine.ruby@example.com", "items": [("YOGA-MAT-PRP", 1)], "payment": "paid"},
    {
        "kind": "late_auto", "customer": "ayumu.hirano@example.com", "items": [("KURTA-M-BLK", 1)],
        "payment": "paid", "days_ago": 12,
    },
    {
        "kind": "late_manager", "customer": "russel.winfield@example.com", "items": [("BLENDER-HAND-01", 1)],
        "payment": "paid", "days_ago": 12,
    },
]

Q_CUSTOMERS = "{ customers(first: 50) { nodes { id defaultEmailAddress { emailAddress } } } }"
Q_VARIANT = "query($q: String!) { productVariants(first: 1, query: $q) { nodes { id sku price } } }"
Q_ORDER_BY_TAG = "query($q: String!) { orders(first: 1, query: $q) { nodes { id name } } }"
M_ORDER_CREATE = """
mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order { id name fulfillmentOrders(first: 5) { nodes { id status } } }
    userErrors { field message }
  }
}
"""
M_FULFILL = """
mutation($f: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $f) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""


def money(amount: Decimal) -> dict[str, Any]:
    return {"shopMoney": {"amount": str(amount), "currencyCode": "PKR"}}


def order_input(
    plan: dict[str, Any], customer_id: str, email: str, variants: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    items = []
    total = Decimal(0)
    for sku, qty in plan["items"]:
        price = Decimal(str(variants[sku]["price"]))
        items.append({"variantId": variants[sku]["id"], "quantity": qty, "priceSet": money(price)})
        total += price * qty
    cod = plan["payment"] == "cod"
    order: dict[str, Any] = {
        "email": email,
        "currency": "PKR",
        "customer": {"toAssociate": {"id": customer_id}},
        "lineItems": items,
        "transactions": [
            {
                "kind": "SALE",
                "status": "PENDING" if cod else "SUCCESS",  # COD: the money is collected at the door
                "gateway": COD_GATEWAY if cod else "manual",
                "amountSet": money(total),
            }
        ],
        "tags": [TAG, f"{TAG}-{plan['kind']}"],
        "note": f"ShopPilot test order ({plan['kind']})",
    }
    if plan.get("days_ago"):  # an old order: the date the order was placed
        placed = datetime.now(UTC) - timedelta(days=plan["days_ago"])
        order["processedAt"] = placed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return order


def main() -> int:
    apply = "--apply" in sys.argv
    backend = ShopifyBackend(
        domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version,
    )
    try:
        customers: dict[str, str] = {}
        for node in backend._gql(Q_CUSTOMERS)["customers"]["nodes"]:
            email = ((node.get("defaultEmailAddress") or {}).get("emailAddress") or "").lower()
            if email:
                customers[email] = node["id"]
        if not customers:
            print("STOP: no customer with a visible e-mail (is the protected customer data access working?)")
            return 1

        variants: dict[str, dict[str, Any]] = {}
        for plan in PLAN:
            for sku, _qty in plan["items"]:
                if sku not in variants:
                    nodes = backend._gql(Q_VARIANT, {"q": f'sku:"{sku}"'})["productVariants"]["nodes"]
                    if not nodes or (nodes[0].get("sku") or "").lower() != sku.lower():
                        print(f"STOP: product {sku} is not in the store (run seed_shopify_products.py first)")
                        return 1
                    variants[sku] = nodes[0]

        print(f"{'kind':<10} {'customer':<32} items")
        todo = []
        for plan in PLAN:
            if plan["customer"] not in customers:
                print(f"STOP: customer {plan['customer']} not found. Customers in the store: {sorted(customers)}")
                return 1
            found = backend._gql(Q_ORDER_BY_TAG, {"q": f"tag:{TAG}-{plan['kind']}"})["orders"]["nodes"]
            items = ", ".join(f"{sku} x{qty}" for sku, qty in plan["items"])
            state = f"exists already as {found[0]['name']}" if found else "to create"
            print(f"{plan['kind']:<10} {plan['customer']:<32} {items}  ({state})")
            if not found:
                todo.append(plan)
        if not apply:
            print("\nDry run: nothing was written. Run again with --apply to create the orders.")
            return 0

        created: list[tuple[str, str]] = []
        for plan in todo:
            customer_id = customers[plan["customer"]]
            options = {"inventoryBehaviour": "BYPASS", "sendReceipt": False, "sendFulfillmentReceipt": False}
            result = backend._gql(
                M_ORDER_CREATE,
                {"order": order_input(plan, customer_id, plan["customer"], variants), "options": options},
            )["orderCreate"]
            if result.get("userErrors"):
                print(f"\nSTOP at {plan['kind']}: {result['userErrors'][:3]}")
                return 1
            order = result["order"]
            if plan["kind"] == "fulfilled":
                fulfillment_orders = [f["id"] for f in order["fulfillmentOrders"]["nodes"] if f["status"] == "OPEN"]
                if not fulfillment_orders:
                    print(f"\nSTOP: {order['name']} has no open fulfillment order, so it cannot be fulfilled")
                    return 1
                fulfilled = backend._gql(
                    M_FULFILL,
                    {
                        "f": {
                            "notifyCustomer": False,
                            "trackingInfo": {"number": TRACKING_NO, "company": COURIER},
                            "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fid} for fid in fulfillment_orders],
                        }
                    },
                )["fulfillmentCreate"]
                if fulfilled.get("userErrors"):
                    print(f"\nSTOP: {order['name']} was created but not fulfilled: {fulfilled['userErrors'][:3]}")
                    return 1
            created.append((order["name"], plan["kind"]))
            print(f"created {order['name']} ({plan['kind']})")

        print("\nDone. Tracking number of the fulfilled order:", TRACKING_NO)
        for name, kind in created:
            print(f"  {name}  {kind}")
        print("Check with: uv run python scripts/check_shopify.py")
        return 0
    except AppError as err:
        print(f"\nFAILED ({err.code}) {err.message} {err.details}")
        return 1
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
