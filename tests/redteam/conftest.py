"""Shared fixture for the red-team tests: a seeded in-memory store plus the app tables. No Docker needed.

Same idea as tests/graph/conftest.py: `env.start(email, role, ticket_id)` sets the run context the way the API layer
does, and `NOW` is fixed, so every run sees the same store. Only the policy search is replaced (it needs pgvector).
The language model is NOT replaced: the attack tests talk to the real model from .env.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from shoppilot.db.models import AppBase, TicketRow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.base import Order
from shoppilot.shop.mock_models import OrderRow
from shoppilot.shop.mockshop import MockShop
from shoppilot.shop.seed import seed_database
from shoppilot.tools.context import RunContext, ctx_var

NOW = datetime(2026, 10, 1, 12, 0, 0)


@pytest.fixture
def env():
    engine = make_engine("sqlite://")
    seed_database(engine, NOW)
    AppBase.metadata.create_all(engine)
    sf = make_session_factory(engine)
    shop = MockShop(sf, now=lambda: NOW)
    tokens = []

    def start(email: str, role: str = "support", ticket_id: str = "T-1") -> RunContext:
        with sf() as s:
            if s.get(TicketRow, ticket_id) is None:
                s.add(TicketRow(id=ticket_id, customer_email=email))
                s.commit()
        ctx = RunContext(
            shop=shop, session_factory=sf, ticket_id=ticket_id, customer_email=email,
            actor_id="u1", actor_role=role, now=lambda: NOW,
        )  # fmt: skip
        tokens.append(ctx_var.set(ctx))
        return ctx

    def orders_of(scenario: str) -> list[Order]:
        with sf() as s:
            names = s.scalars(select(OrderRow.name).where(OrderRow.scenario == scenario).order_by(OrderRow.id)).all()
        return [shop.get_order(n) for n in names]

    yield SimpleNamespace(shop=shop, sf=sf, start=start, orders_of=orders_of)
    for token in reversed(tokens):
        ctx_var.reset(token)
    engine.dispose()
