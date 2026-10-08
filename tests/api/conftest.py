"""Fixtures for the API tests (Step Q): the real routers on a small app, an in-memory database, and a fake graph.

No Docker, no model, no internet. The app is built here (not imported from api/main.py), so the real lifespan, which opens
the Postgres checkpointer, is not needed. Everything the routers read from `app.state` is put there by the fixture.

`api.auth("manager")` gives the headers of a signed token with that role, like a login would.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shoppilot.api.routers import ALL_ROUTERS
from shoppilot.core.config import settings
from shoppilot.core.errors import register_exception_handlers
from shoppilot.core.security import create_token
from shoppilot.db.models import AppBase
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.seed import seed_database

SEED_NOW = datetime(2026, 10, 1, 12, 0, 0)


class FakeGraph:
    """Stands in for the compiled supervisor graph. It has the few methods the API calls.

    pending  the interrupt value the graph is "waiting" on (None: not waiting)
    chunks   what stream() yields, as (namespace, {node: update}) like LangGraph with subgraphs=True
    invoked  every call of invoke(): (input, config). A resume Command lands here.
    """

    def __init__(self) -> None:
        self.pending: dict[str, Any] | None = None
        self.chunks: list[tuple[tuple[str, ...], dict[str, Any]]] = [
            ((), {"classify": {"route": "support", "route_by": "rule"}}),
            (("run_support:1",), {"triage": {"intent": "refund", "order_ref": "#88601"}}),
        ]
        self.invoked: list[tuple[Any, Any]] = []

    def get_state(self, config: Any) -> SimpleNamespace:
        if self.pending is None:  # "the graph never ran" (or it finished): created_at None means no saved state
            return SimpleNamespace(created_at=None, values={}, tasks=[], next=(), metadata={})
        task = SimpleNamespace(interrupts=[SimpleNamespace(value=self.pending)])
        return SimpleNamespace(
            created_at="2026-10-01T12:00:00", values={}, tasks=[task], next=("approval_gate",), metadata={}
        )

    def get_state_history(self, config: Any, limit: int = 50) -> list[Any]:
        return []

    def stream(self, graph_input: Any, config: Any, **kwargs: Any):
        yield from self.chunks

    def invoke(self, graph_input: Any, config: Any) -> dict[str, Any]:
        self.invoked.append((graph_input, config))
        self.pending = None  # the resumed graph ran to its end
        return {}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "store_backend", "mock")  # tests never use the real store, whatever .env says
    engine = make_engine("sqlite://")
    seed_database(engine, SEED_NOW)  # the 200 fake MockShop orders (the simulator picks from them)
    AppBase.metadata.create_all(engine)
    sf = make_session_factory(engine)

    app = FastAPI()
    register_exception_handlers(app)
    for router in ALL_ROUTERS:
        app.include_router(router)
    graph = FakeGraph()
    app.state.session_factory = sf
    app.state.shop = object()  # the fake graph never touches the shop
    app.state.graph = graph

    def auth(role: str) -> dict[str, str]:
        token = create_token(f"{role}-id", f"{role}@example.com", role)
        return {"Authorization": f"Bearer {token}"}

    yield SimpleNamespace(client=TestClient(app), sf=sf, graph=graph, auth=auth, app=app)
    engine.dispose()
