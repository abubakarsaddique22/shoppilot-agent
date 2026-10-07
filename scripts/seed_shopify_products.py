"""Put the 13 ShopPilot products into the Shopify store (Step E). Safe to run again.

    uv run python scripts/seed_shopify_products.py            # dry run: shows what it would create, writes nothing
    uv run python scripts/seed_shopify_products.py --apply    # really creates the products

The list comes from shoppilot/shop/seed.py (PRODUCTS), so MockShop and Shopify have the same catalog.
Each product gets: SKU, price (PKR), vendor, type, tags, stock, and the two metafields the Inventory agent reads
(shoppilot.reorder_point and shoppilot.avg_daily_sales). Products whose SKU is already in the store are skipped.
Every new product also gets the tag "shoppilot-seed", so you can find and delete them in the Shopify admin later.

Needs these scopes: write_products, write_inventory, read_locations (the stock is set at the first active location).
"""
import sys
from typing import Any

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError
from shoppilot.shop.seed import PRODUCTS
from shoppilot.shop.shopify import ShopifyBackend

Q_EXISTING = "{ productVariants(first: 250) { nodes { sku } } }"
Q_LOCATIONS = "{ locations(first: 5) { nodes { id } } }"  # only id: Shopify blocks the name field without read_locations
M_PRODUCT_SET = """
mutation($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product { id title variants(first: 1) { nodes { sku } } }
    userErrors { field message code }
  }
}
"""


def product_input(row: tuple[Any, ...], location_id: str) -> dict[str, Any]:
    sku, title, product_type, vendor, price, on_hand, reorder_point, daily_sales, tags = row
    return {
        "title": title,
        "descriptionHtml": f"<p>{title}</p>",
        "vendor": vendor,
        "productType": product_type,
        "tags": ["shoppilot-seed", *[t.strip() for t in tags.split(",") if t.strip()]],
        "status": "ACTIVE",
        "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
        "variants": [
            {
                "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                "sku": sku,
                "price": f"{price}.00",
                "inventoryItem": {"tracked": True},
                "inventoryQuantities": [{"locationId": location_id, "name": "available", "quantity": on_hand}],
                "metafields": [
                    {"namespace": "shoppilot", "key": "reorder_point", "type": "number_integer", "value": str(reorder_point)},
                    {"namespace": "shoppilot", "key": "avg_daily_sales", "type": "number_decimal", "value": str(daily_sales)},
                ],
            }
        ],
    }


def main() -> int:
    apply = "--apply" in sys.argv
    backend = ShopifyBackend(
        domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version,
    )
    try:
        nodes = backend._gql(Q_EXISTING)["productVariants"]["nodes"]
        existing = {(n.get("sku") or "").lower() for n in nodes if n.get("sku")}
        todo = [row for row in PRODUCTS if row[0].lower() not in existing]

        location_id = ""
        try:
            places = backend._gql(Q_LOCATIONS)["locations"]["nodes"]
            location_id = places[0]["id"]
            print(f"Stock will be set at the first location: {location_id}")
        except (AppError, IndexError) as err:
            print(f"Cannot read the locations ({getattr(err, 'code', 'no location found')}): {getattr(err, 'details', '')}")
            if apply:
                return 1

        print(f"\n{len(PRODUCTS) - len(todo)} product(s) already in the store, {len(todo)} to create:")
        for sku, title, _t, _v, price, on_hand, reorder_point, daily_sales, _tags in todo:
            low = "  <- LOW STOCK" if on_hand <= reorder_point else ""
            print(f"  {sku:<18}{price:>6} PKR  stock {on_hand:>3}  reorder {reorder_point:>3}  daily {daily_sales}{low}  {title}")
        if not apply:
            print("\nDry run: nothing was written. Run again with --apply to create them.")
            return 0

        for row in todo:
            result = backend._gql(M_PRODUCT_SET, {"input": product_input(row, location_id)})["productSet"]
            if result.get("userErrors"):
                print(f"\nSTOP at {row[0]}: {result['userErrors'][:3]}")
                return 1
            print(f"created {row[0]}")
        print("\nDone. Check with: uv run python scripts/inspect_shopify.py")
        return 0
    except AppError as err:
        print(f"\nFAILED ({err.code}) {err.message} {err.details}")
        return 1
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
