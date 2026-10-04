"""Step H: the application tables. Runs on in-memory SQLite, so no Docker is needed."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shoppilot.db.models import (
    EMBEDDING_DIM,
    ActionRow,
    AppBase,
    ApprovalRow,
    MessageRow,
    PolicyChunkRow,
    TicketRow,
    UserRow,
    utcnow,
)
from shoppilot.db.session import make_engine, make_session_factory


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    AppBase.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        s.add(TicketRow(id="T-1", customer_email="ali@example.com"))
        s.commit()
        yield s
    engine.dispose()


def _action(key: str) -> ActionRow:
    return ActionRow(ticket_id="T-1", tool="issue_refund", args_json={"amount_pkr": 5400}, idempotency_key=key)


def test_ticket_defaults(session: Session) -> None:
    t = session.get(TicketRow, "T-1")
    assert t is not None
    assert t.status == "new"
    assert t.channel == "ui"
    assert t.tokens == 0
    assert t.cost_pkr == Decimal("0")


def test_duplicate_idempotency_key_is_rejected(session: Session) -> None:
    session.add(_action("T-1:O-88731:refund"))
    session.commit()
    session.add(_action("T-1:O-88731:refund"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_different_idempotency_keys_are_allowed(session: Session) -> None:
    session.add_all([_action("T-1:O-1:refund"), _action("T-1:O-2:refund")])
    session.commit()
    assert session.query(ActionRow).count() == 2


def test_approval_defaults_to_pending(session: Session) -> None:
    session.add(
        ApprovalRow(
            ticket_id="T-1",
            action="refund",
            payload_json={"amount_pkr": 5400},
            tier="manager",
            expires_at=utcnow() + timedelta(hours=48),
        )
    )
    session.commit()
    assert session.query(ApprovalRow).one().status == "pending"


def test_approval_tier_auto_is_rejected(session: Session) -> None:
    session.add(
        ApprovalRow(ticket_id="T-1", action="refund", payload_json={}, tier="auto", expires_at=utcnow())
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_user_role_is_rejected(session: Session) -> None:
    session.add(UserRow(email="x@example.com", password_hash="hash", role="superuser"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_message_direction_is_checked(session: Session) -> None:
    session.add(MessageRow(ticket_id="T-1", direction="sideways", body="hello"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_policy_chunk_keeps_its_embedding(session: Session) -> None:
    vec = [0.5] * EMBEDDING_DIM
    session.add(PolicyChunkRow(doc="returns", section="Returns > Damaged items", text="...", embedding=vec))
    session.commit()
    session.expire_all()
    assert session.query(PolicyChunkRow).one().embedding == vec
