"""Check the Shopify connection (no secret is printed).

    uv run python scripts/check_shopify.py

It reads the three SHOP_SHOPIFY_* values from .env, asks Shopify for a token (client credentials grant),
and prints: granted scopes, token lifetime, shop name and currency, and how many orders, products and
customers the store has. It also tells you whether the order e-mail is visible (protected customer data).
"""
import sys

import httpx
from dotenv import dotenv_values

API_VERSION = "2026-07"
QUERY = """
{
  shop { name currencyCode }
  ordersCount { count }
  productsCount { count }
  customersCount { count }
  orders(first: 1) { nodes { name email } }
}
"""


def main() -> int:
    env = dotenv_values(".env")
    domain = (env.get("SHOP_SHOPIFY_STORE_DOMAIN") or "").strip()
    client_id = (env.get("SHOP_SHOPIFY_CLIENT_ID") or "").strip()
    secret = (env.get("SHOP_SHOPIFY_CLIENT_SECRET") or "").strip()

    if not (domain and client_id and secret) or client_id.startswith("yahan") or secret.startswith("yahan"):
        print("STOP: .env still has the placeholder text. Put the real Client ID and Client secret after the = sign.")
        return 1
    if " " in client_id or " " in secret or '"' in client_id or '"' in secret:
        print("STOP: the Client ID or secret has a space or a quote mark. Remove it (no quotes, no spaces).")
        return 1

    resp = httpx.post(
        f"https://{domain}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Token request failed: HTTP {resp.status_code}")
        print(resp.text[:300].replace(secret, "***"))
        return 1
    body = resp.json()
    token = body["access_token"]
    print("Token: OK")
    print("Scopes:", body.get("scope"))
    print("Expires in (seconds):", body.get("expires_in"))

    gql = httpx.post(
        f"https://{domain}/admin/api/{API_VERSION}/graphql.json",
        json={"query": QUERY},
        headers={"X-Shopify-Access-Token": token},
        timeout=30,
    )
    data = gql.json()
    if gql.status_code != 200 or "errors" in data:
        print(f"GraphQL problem: HTTP {gql.status_code}")
        print(str(data.get("errors", data))[:600])
        return 1
    d = data["data"]
    print("Shop:", d["shop"]["name"], "| currency:", d["shop"]["currencyCode"])
    print("Orders:", d["ordersCount"]["count"], "| Products:", d["productsCount"]["count"],
          "| Customers:", d["customersCount"]["count"])
    nodes = d["orders"]["nodes"]
    if nodes:
        print("Order e-mail visible:", bool(nodes[0].get("email")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
