"""Real refund and real cancel on the Shopify test store (Step E, items 6 and 7).

    uv run python scripts/check_shopify_writes.py            # dry run: only the checks that can never move money
    uv run python scripts/check_shopify_writes.py --apply    # also makes the real refund and the real cancel

What --apply does (only on the test orders from scripts/seed_shopify_orders.py):
  #1001  refund of 100 PKR with a fixed key, then the SAME key again (must give the same refund, no second one),
         then the same key with another amount (must be refused as an idempotency conflict).
  #1004  real cancel (no refund, no e-mail to the customer). It is printed whether the stock of the product changed.

Safe to run again: the key is fixed, so a second run finds the first refund and does not refund twice. An order that
is already cancelled is reported, not cancelled again. Order #1003 (shipped) must refuse to be cancelled.
"""
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError, IdempotencyConflict
from shoppilot.shop.shopify import ShopifyBackend

REFUND_ORDER = "#1001"
REFUND_PKR = 100
REFUND_KEY = "shoppilot-live-test-1001"
CANCEL_ORDER = "#1004"
CANCEL_SKU = "YOGA-MAT-PRP"  # the product of #1004
SHIPPED_ORDER = "#1003"

failed: list[str] = []


def run(name: str, fn: Callable[[], Any]) -> Any:
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


def expect_error(name: str, fn: Callable[[], Any], code: str) -> None:
    try:
        fn()
        check(name, False, "(no error was raised)")
    except AppError as err:
        check(name, err.code == code, f"({err.code})")


def check_refund(shop: ShopifyBackend, apply: bool) -> None:
    print(f"\n-- refund on {REFUND_ORDER} --")
    # These three are refused before anything is sent to Shopify, so no money can move.
    expect_error(
        "refund above the balance is refused",
        partial(shop.create_refund, REFUND_ORDER, 1_000_000, "shoppilot-live-test-too-big", "test"),
        "REFUND_EXCEEDS_BALANCE",
    )
    expect_error("refund of 0 is refused", partial(shop.create_refund, REFUND_ORDER, 0, "k", "test"), "INVALID_AMOUNT")
    expect_error(
        "refund without a key is refused", partial(shop.create_refund, REFUND_ORDER, REFUND_PKR, " ", "test"), "INVALID_KEY"
    )
    if not apply:
        print("      (dry run: the real refund is skipped)")
        return

    before = run("get_order before", partial(shop.get_order, REFUND_ORDER))
    first = run("create_refund (1st call)", partial(shop.create_refund, REFUND_ORDER, REFUND_PKR, REFUND_KEY, "ShopPilot live test"))
    if before is None or first is None:
        return
    print(f"      refund id {first.id}, {first.amount_pkr} PKR, status {first.status}")
    check("refund has the right amount", first.amount_pkr == REFUND_PKR, f"({first.amount_pkr})")
    middle = run("get_order after the 1st refund", partial(shop.get_order, REFUND_ORDER))
    second = run("create_refund (same key again)", partial(shop.create_refund, REFUND_ORDER, REFUND_PKR, REFUND_KEY, "ShopPilot live test"))
    after = run("get_order after the 2nd call", partial(shop.get_order, REFUND_ORDER))
    if middle is None or second is None or after is None:
        return
    added = middle.refunded_total - before.refunded_total
    check("the 1st call refunded once (or the key was used in an earlier run)", added in (0, REFUND_PKR), f"(added {added} PKR)")
    check("the same key returns the same refund", second.id == first.id, f"({first.id} vs {second.id})")
    check("the 2nd call refunded nothing more", after.refunded_total == middle.refunded_total, f"({middle.refunded_total} -> {after.refunded_total})")
    try:
        shop.create_refund(REFUND_ORDER, REFUND_PKR + 50, REFUND_KEY, "ShopPilot live test")
        check("same key with another amount is refused", False, "(no error was raised)")
    except IdempotencyConflict:
        check("same key with another amount is refused", True)
    except AppError as err:
        check("same key with another amount is refused", False, f"({err.code}) {err.message}")
    print(f"      {REFUND_ORDER} refunded in total: {after.refunded_total} PKR of {after.amount_paid} PKR")


def check_cancel(shop: ShopifyBackend, apply: bool) -> None:
    print("\n-- cancel --")
    expect_error(
        f"a shipped order ({SHIPPED_ORDER}) cannot be cancelled", partial(shop.cancel_order, SHIPPED_ORDER), "ORDER_NOT_CANCELLABLE"
    )
    if not apply:
        print(f"      (dry run: the real cancel of {CANCEL_ORDER} is skipped)")
        return

    order = run(f"get_order {CANCEL_ORDER}", partial(shop.get_order, CANCEL_ORDER))
    stock_before = run("get_inventory before", partial(shop.get_inventory, CANCEL_SKU))
    if order is None or stock_before is None:
        return
    if order.status == "cancelled":
        print(f"OK    {CANCEL_ORDER} is already cancelled (earlier run), nothing to do")
    else:
        cancelled = run(f"cancel_order {CANCEL_ORDER}", partial(shop.cancel_order, CANCEL_ORDER))
        if cancelled is None:
            return
        check(f"{CANCEL_ORDER} is cancelled", cancelled.status == "cancelled", f"({cancelled.status})")
        check("no money was refunded by the cancel", cancelled.refunded_total == order.refunded_total)
    expect_error("cancelling it again is refused", partial(shop.cancel_order, CANCEL_ORDER), "ORDER_NOT_CANCELLABLE")
    stock_after = run("get_inventory after", partial(shop.get_inventory, CANCEL_SKU))
    if stock_after is not None:
        note = "" if stock_after.on_hand == stock_before.on_hand else "  <- the cancel put the item back in stock"
        print(f"      stock of {CANCEL_SKU}: {stock_before.on_hand} -> {stock_after.on_hand}{note}")


def main() -> int:
    apply = "--apply" in sys.argv
    shop = ShopifyBackend(
        domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version,
    )
    print(f"Store: {settings.shopify_store_domain} | {'APPLY: real refund and cancel' if apply else 'dry run'}")
    try:
        check_refund(shop, apply)
        check_cancel(shop, apply)
    finally:
        shop.close()
    print("\nALL OK" if not failed else f"\n{len(failed)} problem(s): {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
