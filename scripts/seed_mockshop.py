"""Fill MockShop with 200 fake orders (Step E). Run with `make seed` after `make up` (Postgres in Docker).

It drops and recreates the MockShop tables only, so you can run it again whenever you want a fresh store.
"""
from shoppilot.db.session import make_engine
from shoppilot.shop.seed import seed_database


def main() -> None:
    counts = seed_database(make_engine())
    print(f"Seeded {sum(counts.values())} orders:")
    for scenario, n in sorted(counts.items()):
        print(f"  {scenario:<26}{n}")


if __name__ == "__main__":
    main()
