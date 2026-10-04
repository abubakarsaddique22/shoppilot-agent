"""Pick SHOP_KB_MIN_SCORE from data (Step G).

    uv run python scripts/check_threshold.py

Prints the best score for real policy questions and for off-topic questions.
The threshold should sit between the two groups: above every off-topic score, below every real score.
"""
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.kb.retriever import search_policy

REAL = [
    "How many days do I have to ask for a refund?",
    "My parcel arrived damaged, what should I send you?",
    "I paid cash on delivery, how do I get my money back?",
    "Who approves a refund of 10000 rupees?",
    "Can I get a refund for a gift card?",
    "Can I exchange my shirt for a bigger size?",
    "You sent me a different product than I ordered",
    "How long does delivery to Karachi take?",
    "Can I change my address after ordering?",
    "How do I track my order?",
]

OFF_TOPIC = [
    "What is the capital of France?",
    "Who won the cricket match yesterday?",
    "Write a poem about the moon",
    "How do I bake a chocolate cake?",
    "What is the price of bitcoin today?",
]


def best_score(session, question: str) -> float:
    result = search_policy(session, question, k=1, min_score=0.0)
    return result.hits[0].score


def main() -> None:
    factory = make_session_factory(make_engine())
    with factory() as session:
        real = [(q, best_score(session, q)) for q in REAL]
        off = [(q, best_score(session, q)) for q in OFF_TOPIC]

    print("REAL questions (top-1 score):")
    for q, s in real:
        print(f"  {s:.3f}  {q}")
    print("\nOFF-TOPIC questions (top-1 score):")
    for q, s in off:
        print(f"  {s:.3f}  {q}")

    lowest_real = min(s for _, s in real)
    highest_off = max(s for _, s in off)
    print(f"\nlowest real score    : {lowest_real:.3f}")
    print(f"highest off-topic    : {highest_off:.3f}")
    if highest_off < lowest_real:
        print(f"suggested kb_min_score: {(lowest_real + highest_off) / 2:.2f}")
    else:
        print("The two groups overlap: no single threshold separates them. Tell me and we will look at it.")


if __name__ == "__main__":
    main()
