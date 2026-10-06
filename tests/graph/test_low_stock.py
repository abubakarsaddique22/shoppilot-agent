"""Low-stock job tests (Step P): drafts for low SKUs, no pile-up, one failure never stops the others.

No Docker, no model, no internet: the seeded in-memory store from conftest.py. The model is switched off, so the
Inventory agent uses its plain suggested quantity.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from shoppilot.agents import inventory as inventory_module
from shoppilot.db.models import ApprovalRow, TicketRow
from shoppilot.jobs import low_stock as job_module
from shoppilot.jobs.low_stock import run_low_stock
from shoppilot.shop.mock_models import InventoryLevelRow, PurchaseOrderDraftRow, VariantRow
from shoppilot.tools.context import ctx_var

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # SQLite and Decimal

NOW = datetime(2026, 10, 1, 12, 0, 0)  # the same fixed clock as conftest.py
DAY = NOW.date().isoformat()


def now() -> datetime:
    return NOW


def tomorrow() -> datetime:
    return NOW + timedelta(days=1)


@pytest.fixture
def stock(env, monkeypatch):
    """The seeded store with the model switched off (the Inventory agent then uses its suggested quantity)."""
    monkeypatch.setattr(inventory_module, "_ask_model", lambda stock, suggested: None)
    return env


# ------------------------------------------------------------------ helpers
def count(env, model, *where) -> int:
    with env.sf() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def skus_with_supplier(env, n: int) -> list[str]:
    """The first n SKUs that have a supplier (a purchase order needs one)."""
    with env.sf() as s:
        skus = s.scalars(select(VariantRow.sku).order_by(VariantRow.sku)).all()
    good = [sku for sku in skus if env.shop.get_product(sku).supplier.strip()]
    assert len(good) >= n, "the seed needs more products with a supplier for this test"
    return good[:n]


def set_stock(env, sku: str, available: int) -> None:
    with env.sf() as s:
        variant = s.scalar(select(VariantRow).where(VariantRow.sku == sku))
        level = s.scalar(select(InventoryLevelRow).where(InventoryLevelRow.inventory_item_id == variant.inventory_item_id))
        level.available = available
        level.reorder_point = 10
        s.commit()


def make_all_fine(env) -> None:
    with env.sf() as s:
        for level in s.scalars(select(InventoryLevelRow)).all():
            level.available = level.reorder_point + 100
        s.commit()


def pending_orders(env) -> int:
    return count(env, ApprovalRow, ApprovalRow.action == "purchase_order", ApprovalRow.status == "pending")


def run(env, when=now) -> dict:
    return run_low_stock(sf=env.sf, shop=env.shop, now=when)


# ------------------------------------------------------------------ the job
def test_every_low_sku_gets_a_draft_and_an_approval(stock):
    skus = skus_with_supplier(stock, 3)  # three SKUs: more than the 2 writes one run context is allowed
    for sku in skus:
        set_stock(stock, sku, 0)
    expected = {i.sku for i in stock.shop.sales_summary(NOW.date()).low_stock}

    result = run(stock)

    assert set(skus) <= set(result["drafted"])  # each SKU has its own tool budget
    assert result["checked"] == len(expected)
    assert set(result["drafted"]) | set(result["skipped"]) | set(result["failed"]) == expected  # nothing is lost
    assert count(stock, PurchaseOrderDraftRow) == len(result["drafted"])
    assert pending_orders(stock) == len(result["drafted"])
    with stock.sf() as s:
        ticket = s.get(TicketRow, f"low-stock-{DAY}")
    assert ticket is not None and ticket.channel == "scheduler"


def test_the_drafts_are_only_drafts_for_a_manager(stock):
    sku = skus_with_supplier(stock, 1)[0]
    set_stock(stock, sku, 0)
    run(stock)
    with stock.sf() as s:
        draft = s.scalar(select(PurchaseOrderDraftRow).where(PurchaseOrderDraftRow.sku == sku))
        approval = s.scalar(select(ApprovalRow).where(ApprovalRow.action == "purchase_order"))
    assert draft.status == "draft" and draft.qty > 0  # never sent to the supplier
    assert approval.tier == "manager" and approval.status == "pending"


def test_a_second_run_on_the_same_day_makes_no_new_draft(stock):
    sku = skus_with_supplier(stock, 1)[0]
    set_stock(stock, sku, 0)
    first = run(stock)
    drafts = count(stock, PurchaseOrderDraftRow)
    again = run(stock)
    assert sku in first["drafted"] and again["drafted"] == [] and sku in again["skipped"]
    assert count(stock, PurchaseOrderDraftRow) == drafts


def test_the_next_day_does_not_pile_up_while_a_person_has_not_looked(stock):
    sku = skus_with_supplier(stock, 1)[0]
    set_stock(stock, sku, 0)
    run(stock)
    drafts = count(stock, PurchaseOrderDraftRow)
    next_day = run(stock, tomorrow)
    assert sku in next_day["skipped"] and next_day["drafted"] == []
    assert count(stock, PurchaseOrderDraftRow) == drafts


def test_after_the_approval_is_decided_a_new_order_can_follow(stock):
    sku = skus_with_supplier(stock, 1)[0]
    set_stock(stock, sku, 0)
    run(stock)
    with stock.sf() as s:
        for row in s.scalars(select(ApprovalRow).where(ApprovalRow.action == "purchase_order")).all():
            row.status = "approved"  # a manager looked at it
        s.commit()
    drafts = count(stock, PurchaseOrderDraftRow)
    next_day = run(stock, tomorrow)
    assert sku in next_day["drafted"]
    assert count(stock, PurchaseOrderDraftRow) > drafts


def test_when_stock_is_fine_nothing_happens(stock):
    make_all_fine(stock)
    result = run(stock)
    assert result == {"checked": 0, "drafted": [], "skipped": [], "failed": {}}
    assert count(stock, PurchaseOrderDraftRow) == 0
    assert count(stock, TicketRow, TicketRow.id == f"low-stock-{DAY}") == 0  # no ticket for an empty check


def test_one_failing_sku_does_not_stop_the_others(stock, monkeypatch):
    make_all_fine(stock)
    first, second = skus_with_supplier(stock, 2)
    set_stock(stock, first, 0)
    set_stock(stock, second, 0)

    def fake_invoke(state):
        if state["sku"] == first:
            raise RuntimeError("the agent broke")
        return {"draft": {"draft_id": 1}}

    monkeypatch.setattr(job_module, "build_inventory_graph", lambda: SimpleNamespace(invoke=fake_invoke))
    result = run(stock)
    assert result["failed"] == {first: "RuntimeError"}
    assert result["drafted"] == [second]


def test_a_sku_without_a_draft_is_reported_as_failed(stock, monkeypatch):
    make_all_fine(stock)
    sku = skus_with_supplier(stock, 1)[0]
    set_stock(stock, sku, 0)
    monkeypatch.setattr(job_module, "build_inventory_graph", lambda: SimpleNamespace(invoke=lambda state: {"errors": ["NO_SUPPLIER"]}))
    result = run(stock)
    assert result["failed"] == {sku: "NO_SUPPLIER"} and result["drafted"] == []


def test_the_job_leaves_no_run_context_behind(stock):
    make_all_fine(stock)
    set_stock(stock, skus_with_supplier(stock, 1)[0], 0)
    run(stock)
    assert ctx_var.get(None) is None
