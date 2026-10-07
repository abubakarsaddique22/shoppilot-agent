"""Read-only look at the Shopify store (Step E). It writes NOTHING and prints no secret.

    uv run python scripts/inspect_shopify.py

Prints the variants (SKU, price, stock, the two shoppilot metafields), the customers and the locations,
so the test-data script can be written for the real store and not for a guess.
Each part runs on its own: if one part fails (for example a missing scope), the others still print.
"""
import sys
from typing import Any

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError
from shoppilot.shop.shopify import ShopifyBackend

Q_VARIANTS = """
{
  productVariants(first: 50) {
    nodes {
      sku title price
      product { title status tags }
      inventoryItem { tracked inventoryLevels(first: 5) { nodes { quantities(names: ["available"]) { quantity } } } }
      reorderPoint: metafield(namespace: "shoppilot", key: "reorder_point") { value }
      avgDailySales: metafield(namespace: "shoppilot", key: "avg_daily_sales") { value }
    }
  }
}
"""
Q_CUSTOMERS = """
{
  customers(first: 20) {
    nodes { id firstName lastName tags defaultEmailAddress { emailAddress } defaultAddress { city } }
  }
}
"""
Q_LOCATIONS = "{ locations(first: 10) { nodes { id name isActive fulfillsOnlineOrders } } }"


def ask(backend: ShopifyBackend, title: str, query: str) -> dict[str, Any] | None:
    try:
        return backend._gql(query)
    except AppError as err:
        print(f"\n{title}: FAILED ({err.code}) {err.message} {err.details}")
        return None


def show_variants(data: dict[str, Any]) -> None:
    nodes = data["productVariants"]["nodes"]
    print(f"\nVARIANTS ({len(nodes)})")
    print(f"{'sku':<18}{'price':>8} {'stock':>6} {'reorder':>8} {'daily':>6}  {'status':<7} title | tags")
    for v in nodes:
        levels = ((v.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("nodes") or []
        stock = sum(int(q["quantity"]) for lv in levels for q in lv.get("quantities") or []) if levels else "-"
        reorder = (v.get("reorderPoint") or {}).get("value") or "-"
        daily = (v.get("avgDailySales") or {}).get("value") or "-"
        p = v["product"]
        tags = ",".join(p.get("tags") or [])
        print(
            f"{(v.get('sku') or '(no sku)'):<18}{v.get('price', ''):>8} {stock!s:>6} {reorder:>8} {daily:>6}  "
            f"{p.get('status', ''):<7} {p.get('title', '')} | {tags}"
        )


def show_customers(data: dict[str, Any]) -> None:
    nodes = data["customers"]["nodes"]
    print(f"\nCUSTOMERS ({len(nodes)})")
    for c in nodes:
        email = (c.get("defaultEmailAddress") or {}).get("emailAddress") or "(no email visible)"
        city = (c.get("defaultAddress") or {}).get("city") or "-"
        name = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()
        print(f"{c['id'].rsplit('/', 1)[-1]:<14}{name:<22}{email:<34}{city:<12}{','.join(c.get('tags') or [])}")


def show_locations(data: dict[str, Any]) -> None:
    nodes = data["locations"]["nodes"]
    print(f"\nLOCATIONS ({len(nodes)})")
    for loc in nodes:
        print(f"{loc['id']}  {loc['name']}  active={loc['isActive']}  fulfillsOnlineOrders={loc['fulfillsOnlineOrders']}")


def main() -> int:
    backend = ShopifyBackend(
        domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version,
    )
    for title, query, show in (
        ("variants", Q_VARIANTS, show_variants),
        ("customers", Q_CUSTOMERS, show_customers),
        ("locations", Q_LOCATIONS, show_locations),
    ):
        data = ask(backend, title, query)
        if data is not None:
            show(data)
    backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
