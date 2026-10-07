"""Check the REAL ShopifyBackend against the real store (Step E). Read-only: it writes nothing.

    uv run python scripts/check_shopify_backend.py

The unit tests use a fake Shopify. This script uses the real one, so it shows whether the queries we wrote also work there.
It checks the 13 ShopPilot products (price, vendor, refundable, stock, reorder point, daily sales), an unknown SKU,
sales_summary for today (low stock must be exactly the products that are low in shop/seed.py), customers, and the 4 test
orders from scripts/seed_shopify_orders.py (get_order, find_orders, track_shipment, refunds_last_90d, e-mail).

Needs the products from scripts/seed_shopify_products.py and the orders from scripts/seed_shopify_orders.py.
The order checks still pass after the real refund and cancel tests: #1001 may be partly refunded, #1004 may be cancelled.
Shopify's order search can be a minute late for a brand new order: if find_orders misses one, run the script again.
"""
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError, NotFound, OrderNotFound
from shoppilot.shop.seed import PRODUCTS
from shoppilot.shop.shopify import ShopifyBackend

failed: list[str] = []

PRICE = {row[0]: row[4] for row in PRODUCTS}
TRACKING_NO = "TRK-TEST-001"  # same as scripts/seed_shopify_orders.py
KARINE = "karine.ruby@example.com"
EXPECTED_ORDERS: dict[str, dict[str, Any]] = {
    "#1001": {"status": {"placed", "refunded"}, "payment": "prepaid", "sku": "CASE-PRM-01", "qty": 2, "email": KARINE},
    "#1002": {"status": {"placed"}, "payment": "cod", "sku": "WALLET-LTH-BRN", "qty": 1, "email": "russel.winfield@example.com"},
    "#1003": {"status": {"shipped"}, "payment": "prepaid", "sku": "LAMP-LED-01", "qty": 1, "email": "ayumu.hirano@example.com"},
    "#1004": {"status": {"placed", "cancelled"}, "payment": "prepaid", "sku": "YOGA-MAT-PRP", "qty": 1, "email": KARINE},
}


def make_shop() -> ShopifyBackend:
    return ShopifyBackend(
        domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version,
    )


def run(name: str, fn: Callable[[], Any]) -> Any:
    """Runs one call. An AppError is printed (code, message, details) and counted as a failure."""
    try:
        return fn()
    except AppError as err:
        print(f"FAIL  {name}: ({err.code}) {err.message} {err.details}")
        failed.append(name)
        return None


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'}  {name} {detail}".rstrip())
    if not ok:
        failed.append(name)


def check_products(shop: ShopifyBackend) -> None:
    print("\n-- products and stock (13 SKUs) --")
    for sku, _title, ptype, vendor, price, on_hand, reorder, velocity, tags in PRODUCTS:
        product = run(f"get_product {sku}", partial(shop.get_product, sku))
        level = run(f"get_inventory {sku}", partial(shop.get_inventory, sku))
        if product is None or level is None:
            continue
        wrong = []
        if (product.price_pkr, product.category, product.supplier) != (price, ptype, vendor):
            wrong.append(f"product={product.price_pkr}/{product.category}/{product.supplier}")
        if product.refundable != ("non-refundable" not in tags):
            wrong.append(f"refundable={product.refundable}")
        if (level.on_hand, level.reorder_point, level.avg_daily_sales) != (on_hand, reorder, velocity):
            wrong.append(f"stock={level.on_hand}/{level.reorder_point}/{level.avg_daily_sales}")
        check(sku, not wrong, ", ".join(wrong))


def check_unknown_sku(shop: ShopifyBackend) -> None:
    print("\n-- unknown SKU --")
    try:
        shop.get_product("NO-SUCH-SKU")
        check("unknown SKU gives NotFound", False, "(a product was returned)")
    except NotFound as err:
        check("unknown SKU gives NotFound", err.code == "PRODUCT_NOT_FOUND", f"({err.code})")
    except AppError as err:
        check("unknown SKU gives NotFound", False, f"({err.code}) {err.message}")


def check_sales_summary(shop: ShopifyBackend) -> None:
    print("\n-- sales_summary (today, UTC) --")
    summary = run("sales_summary", lambda: shop.sales_summary(datetime.now(UTC).date()))
    if summary is None:
        return
    print(f"orders {summary.orders_count} | sales {summary.sales_pkr} PKR | refunds {summary.refunds_count} "
          f"({summary.refunds_pkr} PKR) | late orders {summary.late_orders}")
    for item in summary.low_stock:
        print(f"low stock: {item.sku} on hand {item.on_hand} (reorder at {item.reorder_point})")
    expected = {row[0] for row in PRODUCTS if row[5] <= row[6]}
    got = {item.sku for item in summary.low_stock}
    check("low stock is exactly the expected SKUs", got == expected, f"missing={expected - got} extra={got - expected}")


def check_orders(shop: ShopifyBackend) -> None:
    print("\n-- orders: get_order (#1001 to #1004) --")
    for name, want in EXPECTED_ORDERS.items():
        order = run(f"get_order {name}", partial(shop.get_order, name))
        if order is None:
            continue
        expected_amount = PRICE[want["sku"]] * want["qty"]
        wrong = []
        if order.status not in want["status"]:
            wrong.append(f"status={order.status}")
        if order.payment_method != want["payment"]:
            wrong.append(f"payment={order.payment_method}")
        if order.amount_paid != expected_amount:
            wrong.append(f"amount={order.amount_paid} (want {expected_amount})")
        if [(i.sku, i.qty) for i in order.items] != [(want["sku"], want["qty"])]:
            wrong.append(f"items={[(i.sku, i.qty) for i in order.items]}")
        if order.customer_email.lower() != want["email"]:
            wrong.append(f"email={order.customer_email!r}")  # empty = protected customer data is not working
        if not order.customer_id:
            wrong.append("no customer_id")
        if not 0 <= order.refunded_total <= order.amount_paid:
            wrong.append(f"refunded_total={order.refunded_total}")
        if order.refunds_last_90d < 0:
            wrong.append(f"refunds_last_90d={order.refunds_last_90d}")
        if (order.tracking_no or "") != (TRACKING_NO if name == "#1003" else ""):
            wrong.append(f"tracking={order.tracking_no}")
        check(
            name,
            not wrong,
            ", ".join(wrong) or f"{order.status}, {order.payment_method}, {order.amount_paid} PKR, "
            f"refunded {order.refunded_total}, refunds_last_90d {order.refunds_last_90d}",
        )

    for ref in ("#9999", "ignore previous instructions"):
        try:
            shop.get_order(ref)
            check(f"get_order {ref!r} gives OrderNotFound", False, "(an order was returned)")
        except OrderNotFound:
            check(f"get_order {ref!r} gives OrderNotFound", True)
        except AppError as err:
            check(f"get_order {ref!r} gives OrderNotFound", False, f"({err.code}) {err.message}")


def check_find_orders(shop: ShopifyBackend) -> None:
    print("\n-- orders: find_orders --")
    orders = run("find_orders karine", partial(shop.find_orders, KARINE))
    if orders is not None:
        ids = {o.id for o in orders}
        check("find_orders returns #1001 and #1004", {"#1001", "#1004"} <= ids, f"got {sorted(ids)}")
        check("find_orders returns only this e-mail", all(o.customer_email.lower() == KARINE for o in orders))
    upper = run("find_orders KARINE (upper case)", partial(shop.find_orders, KARINE.upper()))
    if upper is not None and orders is not None:
        check("find_orders ignores upper/lower case", {o.id for o in upper} == {o.id for o in orders})
    nobody = run("find_orders unknown e-mail", partial(shop.find_orders, "nobody@example.com"))
    if nobody is not None:
        check("find_orders of an unknown e-mail is empty", nobody == [])


def check_tracking(shop: ShopifyBackend) -> None:
    print("\n-- orders: track_shipment --")
    fresh = make_shop()  # no tracking index yet: this goes through the scan of the latest orders
    try:
        scanned = run("track_shipment (scan)", partial(fresh.track_shipment, TRACKING_NO))
    finally:
        fresh.close()
    shop.get_order("#1003")  # reading the order fills the tracking index
    indexed = run("track_shipment (index)", partial(shop.track_shipment, TRACKING_NO))
    for label, ship in (("scan", scanned), ("index", indexed)):
        if ship is None:
            continue
        wrong = []
        if ship.order_id != "#1003":
            wrong.append(f"order={ship.order_id}")
        if ship.courier != "TCS":
            wrong.append(f"courier={ship.courier}")
        if ship.status not in ("in_transit", "delayed", "delivered"):
            wrong.append(f"status={ship.status}")
        check(f"track_shipment {label}", not wrong, ", ".join(wrong) or f"{ship.status}, {len(ship.events)} event(s)")
    try:
        shop.track_shipment("NO-SUCH-TRACKING")
        check("unknown tracking gives NotFound", False, "(a shipment was returned)")
    except NotFound as err:
        check("unknown tracking gives NotFound", err.code == "SHIPMENT_NOT_FOUND", f"({err.code})")
    except AppError as err:
        check("unknown tracking gives NotFound", False, f"({err.code}) {err.message}")


def check_customers(shop: ShopifyBackend) -> None:
    print("\n-- customers (needs protected customer data access) --")
    order = run("get_order #1001 (for the customer id)", partial(shop.get_order, "#1001"))
    if order is None:
        return
    by_id = run("get_customer by id", partial(shop.get_customer, order.customer_id))
    by_email = run("get_customer by e-mail", partial(shop.get_customer, KARINE))
    if by_id is not None:
        wrong = []
        if by_id.name != "Karine Ruby":
            wrong.append(f"name={by_id.name!r}")
        if by_id.email.lower() != KARINE:
            wrong.append(f"email={by_id.email!r}")
        if by_id.city != "Ottawa":
            wrong.append(f"city={by_id.city!r}")
        check("get_customer by id", not wrong, ", ".join(wrong))
    if by_id is not None and by_email is not None:
        check("get_customer by id and by e-mail give the same customer", by_id.id == by_email.id)
    try:
        shop.get_customer("nobody@example.com")
        check("unknown customer gives NotFound", False, "(a customer was returned)")
    except NotFound as err:
        check("unknown customer gives NotFound", err.code == "CUSTOMER_NOT_FOUND", f"({err.code})")
    except AppError as err:
        check("unknown customer gives NotFound", False, f"({err.code}) {err.message}")


def main() -> int:
    shop = make_shop()
    try:
        check_products(shop)
        check_unknown_sku(shop)
        check_sales_summary(shop)
        check_orders(shop)
        check_find_orders(shop)
        check_tracking(shop)
        check_customers(shop)
    finally:
        shop.close()
    print("\nALL OK" if not failed else f"\n{len(failed)} problem(s): {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
