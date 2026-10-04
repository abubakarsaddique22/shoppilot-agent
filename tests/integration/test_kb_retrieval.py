"""Step G "done when": ten policy questions return the right section in the top 3.

Needs Postgres (docker compose up -d db, then alembic upgrade head) and the real embedding model.
The first run downloads the model into .cache/fastembed, so it takes a while.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.kb.ingest import ingest_policies
from shoppilot.kb.retriever import search_policy

CASES = [
    ("How many days do I have to ask for a refund?", "Returns > Refund window"),
    ("My parcel arrived damaged, what should I send you?", "Returns > Damaged items"),
    ("I paid cash on delivery, how do I get my money back?", "Returns > Cash on delivery refunds"),
    ("Who approves a refund of 10000 rupees?", "Returns > Refund approval limits"),
    ("Can I get a refund for a gift card?", "Returns > Non-refundable items"),
    ("Can I exchange my shirt for a bigger size?", "Exchange > Wrong size or colour"),
    ("You sent me a different product than I ordered", "Exchange > Wrong item sent"),
    ("How long does delivery to Karachi take?", "Shipping > Delivery times"),
    ("Can I change my address after ordering?", "Shipping > Address changes"),
    ("How do I track my order?", "Shipping > Tracking"),
]


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    engine = make_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except OperationalError:
        pytest.skip("Postgres is not running (docker compose up -d db)")
    factory = make_session_factory(engine)
    ingest_policies(factory)  # real embedder; needs `alembic upgrade head` to have run
    with factory() as s:
        yield s
    engine.dispose()


@pytest.mark.parametrize(("question", "expected"), CASES, ids=[c[1] for c in CASES])
def test_right_section_is_in_the_top_3(session: Session, question: str, expected: str) -> None:
    result = search_policy(session, question, k=3, min_score=0.0)
    assert expected in [h.section for h in result.hits], [(h.section, h.score) for h in result.hits]


def test_off_topic_question_scores_lower_than_every_real_question(session: Session) -> None:
    best_real = min(search_policy(session, q, k=1, min_score=0.0).hits[0].score for q, _ in CASES)
    off_topic = search_policy(session, "What is the capital of France?", k=1, min_score=0.0).hits[0].score
    assert off_topic < best_real, f"off-topic {off_topic} vs weakest real match {best_real}"


def test_a_very_high_threshold_gives_no_policy_found(session: Session) -> None:
    result = search_policy(session, "What is the capital of France?", min_score=0.99)
    assert not result.ok
    assert result.error == "NO_POLICY_FOUND"
    assert result.hits == []


def test_empty_question_is_rejected(session: Session) -> None:
    assert search_policy(session, "   ").error == "EMPTY_QUERY"
