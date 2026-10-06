"""All routers of the API (Step Q). api/main.py includes them in this order.

The order of the imports matters a little: approvals.py uses the run lock from runs.py, so runs is imported first.
Tests build a small app from the same list (tests/api/conftest.py).
"""
from __future__ import annotations

from fastapi import APIRouter

from shoppilot.api.routers import auth, feedback, health, runs, tickets  # isort: skip
from shoppilot.api.routers import approvals, reports, simulator, webhooks  # isort: skip

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    auth.router,
    tickets.router,
    runs.router,
    approvals.router,
    reports.router,
    simulator.router,
    webhooks.router,
    feedback.router,
]

__all__ = ["ALL_ROUTERS"]
