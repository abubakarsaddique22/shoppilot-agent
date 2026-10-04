"""Load the policy documents into the knowledge base (Step G). Run it again whenever configs/policies/*.md changes.

    uv run python scripts/ingest_policies.py
    uv run python scripts/ingest_policies.py --search "how long does delivery take"

--search shows the best matches with their scores, which helps you pick SHOP_KB_MIN_SCORE.
"""
import argparse

from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.kb.ingest import ingest_policies
from shoppilot.kb.retriever import search_policy


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the policy documents, and optionally try a search.")
    parser.add_argument("--search", metavar="QUESTION", help="after indexing, show the best matches for this question")
    args = parser.parse_args()

    factory = make_session_factory(make_engine())
    counts = ingest_policies(factory)
    print(f"Indexed {sum(counts.values())} chunks:")
    for doc, n in counts.items():
        print(f"  {doc:<12}{n}")

    if args.search:
        with factory() as session:
            result = search_policy(session, args.search, k=5, min_score=0.0)
        print(f'\nBest matches for "{args.search}":')
        for hit in result.hits:
            print(f"  {hit.score:.3f}  {hit.section}")


if __name__ == "__main__":
    main()
