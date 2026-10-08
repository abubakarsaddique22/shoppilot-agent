"""API tests (Step Q): login, tickets, run stream, approvals, simulator, webhook, reports, feedback.

They use the real routers with an in-memory database and a fake graph (tests/api/conftest.py). The point is the
rules of the API: who may do what, that a decision is recorded and then resumes the graph, and that doubles do nothing.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from langgraph.types import Command
from sqlalchemy import select

from shoppilot.core.config import settings
from shoppilot.core.security import hash_password
from shoppilot.db.models import ApprovalRow, AuditLogRow, TicketRow, UserRow, utcnow

PASSWORD = "a long password"


# ------------------------------------------------------------------ helpers
def add_user(api, role: str = "manager") -> str:
    email = f"{role}@example.com"
    with api.sf() as s:
        s.add(UserRow(email=email, password_hash=hash_password(PASSWORD), role=role))
        s.commit()
    return email


def add_pending_approval(api, *, tier: str = "manager", amount: int = 5400, ticket_id: str = "T-2001", hours: int = 48) -> int:
    """A ticket waiting for a human, an open approval, and the fake graph waiting on it."""
    with api.sf() as s:
        s.add(TicketRow(id=ticket_id, customer_email="ali@example.com", status="waiting_approval"))
        s.flush()
        row = ApprovalRow(
            ticket_id=ticket_id,
            action="refund",
            payload_json={"amount_pkr": amount, "key": f"{ticket_id}:refund"},
            tier=tier,
            status="pending",
            requested_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=hours),
        )
        s.add(row)
        s.commit()
        approval_id = row.id
    api.graph.pending = {"type": "refund_approval", "ticket_id": ticket_id, "tier": tier, "approval_id": approval_id}
    return approval_id


def decision(note: str = "Courier confirmed the delay", status: str = "approved", **extra) -> dict:
    return {"status": status, "note": note, **extra}


def new_ticket(api, body: str = "Mera order #88601 late hai") -> str:
    res = api.client.post(
        "/v1/tickets", json={"customer_email": "Ali@Example.com", "body": body}, headers=api.auth("support")
    )
    assert res.status_code == 201, res.text
    return res.json()["ticket_id"]


# ------------------------------------------------------------------ auth
def test_login_gives_a_token_and_me_reads_it(api):
    email = add_user(api, "manager")
    res = api.client.post("/v1/auth/login", json={"email": email.upper(), "password": PASSWORD})
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "manager" and body["token_type"] == "bearer"

    me = api.client.get("/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["role"] == "manager"


def test_wrong_password_and_unknown_email_look_the_same(api):
    email = add_user(api, "manager")
    wrong = api.client.post("/v1/auth/login", json={"email": email, "password": "not the password"})
    unknown = api.client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "UNAUTHENTICATED"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_endpoints_need_a_login(api):
    assert api.client.get("/v1/tickets").status_code == 401
    assert api.client.get("/v1/approvals").status_code == 401
    assert api.client.get("/v1/tickets", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_health_is_public_and_ready_checks_database_and_graph(api):
    assert api.client.get("/health").json() == {"status": "ok"}
    ready = api.client.get("/ready")
    assert ready.status_code == 200 and ready.json()["graph"] == "ok"


# ------------------------------------------------------------------ tickets
def test_viewer_can_read_but_not_create(api):
    denied = api.client.post("/v1/tickets", json={"customer_email": "a@b.com", "body": "hi"}, headers=api.auth("viewer"))
    assert denied.status_code == 403
    assert api.client.get("/v1/tickets", headers=api.auth("viewer")).status_code == 200


def test_support_creates_lists_and_reads_a_ticket(api):
    ticket_id = new_ticket(api)
    assert ticket_id.startswith("T-")

    listed = api.client.get("/v1/tickets", headers=api.auth("viewer")).json()
    assert [t["id"] for t in listed] == [ticket_id]
    assert listed[0]["customer_email"] == "ali@example.com"  # stored in lower case

    detail = api.client.get(f"/v1/tickets/{ticket_id}", headers=api.auth("viewer")).json()
    assert detail["ticket"]["status"] == "new"
    assert detail["messages"][0]["direction"] == "inbound"
    assert api.client.get("/v1/tickets/T-9999", headers=api.auth("viewer")).status_code == 404


def test_run_streams_events_and_settles_the_ticket(api):
    ticket_id = new_ticket(api)
    res = api.client.post(f"/v1/tickets/{ticket_id}/run", headers=api.auth("support"))
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    text = res.text
    assert "event: start" in text and "event: node" in text and "event: end" in text
    assert '"status": "finished"' in text

    ticket = api.client.get(f"/v1/tickets/{ticket_id}", headers=api.auth("viewer")).json()["ticket"]
    assert ticket["status"] == "done" and ticket["intent"] == "refund"


def test_viewer_cannot_run_a_ticket(api):
    ticket_id = new_ticket(api)
    assert api.client.post(f"/v1/tickets/{ticket_id}/run", headers=api.auth("viewer")).status_code == 403


def test_run_reports_the_pending_approval_instead_of_running_again(api):
    approval_id = add_pending_approval(api)
    res = api.client.post("/v1/tickets/T-2001/run", headers=api.auth("support"))
    assert res.status_code == 200
    assert "event: interrupt" in res.text and f'"approval_id": {approval_id}' in res.text
    assert '"status": "waiting_approval"' in res.text


# ------------------------------------------------------------------ approvals
def test_support_cannot_open_the_approval_inbox(api):
    assert api.client.get("/v1/approvals", headers=api.auth("support")).status_code == 403
    assert api.client.get("/v1/approvals", headers=api.auth("viewer")).status_code == 403


def test_inbox_shows_only_the_tiers_a_role_may_decide(api):
    manager_id = add_pending_approval(api, tier="manager", ticket_id="T-2001")
    owner_id = add_pending_approval(api, tier="owner", ticket_id="T-2002")

    as_manager = api.client.get("/v1/approvals", headers=api.auth("manager")).json()
    as_owner = api.client.get("/v1/approvals", headers=api.auth("owner")).json()
    assert [a["id"] for a in as_manager] == [manager_id]
    assert sorted(a["id"] for a in as_owner) == sorted([manager_id, owner_id])
    # an approval of a tier you cannot see looks like "not found"
    assert api.client.get(f"/v1/approvals/{owner_id}", headers=api.auth("manager")).status_code == 404


def test_manager_approves_and_the_graph_resumes(api):
    email = "manager@example.com"
    approval_id = add_pending_approval(api)
    res = api.client.post(
        f"/v1/approvals/{approval_id}/decision", json=decision(amount_pkr=5000), headers=api.auth("manager")
    )
    assert res.status_code == 200, res.text
    assert res.json()["graph_status"] == "resumed"
    assert res.json()["events_url"] == "/v1/tickets/T-2001/run"

    # the graph got a resume Command (only a marker: the real decision is in the approvals table)
    (graph_input, _config), = api.graph.invoked
    assert isinstance(graph_input, Command) and graph_input.resume == {"status": "approved"}

    with api.sf() as s:
        row = s.get(ApprovalRow, approval_id)
        assert row.status == "approved" and row.decided_by == email and row.payload_json["amount_pkr"] == 5000
        events = s.scalars(select(AuditLogRow.event)).all()
        ticket = s.get(TicketRow, "T-2001")
    assert "approval_decided" in events
    assert ticket.status == "done"


def test_a_rejection_is_recorded_and_resumes_too(api):
    approval_id = add_pending_approval(api)
    res = api.client.post(
        f"/v1/approvals/{approval_id}/decision", json=decision(status="rejected"), headers=api.auth("manager")
    )
    assert res.status_code == 200 and res.json()["graph_status"] == "resumed"
    assert api.graph.invoked[0][0].resume == {"status": "rejected"}


def test_a_note_is_mandatory_and_the_amount_cannot_go_up(api):
    approval_id = add_pending_approval(api, amount=5400)
    url = f"/v1/approvals/{approval_id}/decision"
    assert api.client.post(url, json={"status": "approved", "note": "  "}, headers=api.auth("manager")).status_code == 422
    higher = api.client.post(url, json=decision(amount_pkr=9000), headers=api.auth("manager"))
    assert higher.status_code == 422
    assert not api.graph.invoked  # nothing was resumed
    with api.sf() as s:
        assert s.get(ApprovalRow, approval_id).status == "pending"


def test_a_manager_cannot_decide_an_owner_tier_approval(api):
    approval_id = add_pending_approval(api, tier="owner", amount=20000)
    res = api.client.post(f"/v1/approvals/{approval_id}/decision", json=decision(), headers=api.auth("manager"))
    assert res.status_code == 403
    assert not api.graph.invoked
    with api.sf() as s:
        assert s.get(ApprovalRow, approval_id).status == "pending"
        assert "approval_forbidden" in s.scalars(select(AuditLogRow.event)).all()

    ok = api.client.post(f"/v1/approvals/{approval_id}/decision", json=decision(), headers=api.auth("owner"))
    assert ok.status_code == 200 and ok.json()["graph_status"] == "resumed"


def test_admin_can_read_but_not_decide(api):
    approval_id = add_pending_approval(api)
    assert api.client.get("/v1/approvals", headers=api.auth("admin")).status_code == 200
    res = api.client.post(f"/v1/approvals/{approval_id}/decision", json=decision(), headers=api.auth("admin"))
    assert res.status_code == 403


def test_a_second_decision_changes_nothing(api):
    approval_id = add_pending_approval(api)
    url = f"/v1/approvals/{approval_id}/decision"
    assert api.client.post(url, json=decision(), headers=api.auth("manager")).status_code == 200
    again = api.client.post(url, json=decision(status="rejected"), headers=api.auth("manager"))
    assert again.status_code == 409
    assert len(api.graph.invoked) == 1
    with api.sf() as s:
        assert s.get(ApprovalRow, approval_id).status == "approved"


def test_an_expired_approval_cannot_be_decided(api):
    approval_id = add_pending_approval(api, hours=-1)
    assert api.client.get("/v1/approvals", headers=api.auth("manager")).json() == []  # not pending any more
    expired = api.client.get("/v1/approvals?status=expired", headers=api.auth("manager")).json()
    assert [a["id"] for a in expired] == [approval_id] and expired[0]["status"] == "expired"
    res = api.client.post(f"/v1/approvals/{approval_id}/decision", json=decision(), headers=api.auth("manager"))
    assert res.status_code == 422


def test_the_decision_is_kept_when_the_graph_is_not_waiting(api):
    approval_id = add_pending_approval(api)
    api.graph.pending = None  # for example a restart lost the checkpoint, or another run already moved on
    res = api.client.post(f"/v1/approvals/{approval_id}/decision", json=decision(), headers=api.auth("manager"))
    assert res.status_code == 200 and res.json()["graph_status"] == "recorded"
    assert not api.graph.invoked
    with api.sf() as s:
        assert s.get(ApprovalRow, approval_id).status == "approved"


def test_deciding_a_missing_approval_is_404(api):
    res = api.client.post("/v1/approvals/9999/decision", json=decision(), headers=api.auth("manager"))
    assert res.status_code == 404 and res.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


# ------------------------------------------------------------------ simulator
@pytest.mark.parametrize("preset", ["late_order", "damaged_item", "injection_attempt"])
def test_simulator_makes_a_ticket_for_a_seeded_customer(api, preset):
    res = api.client.post("/v1/simulator", json={"preset": preset}, headers=api.auth("support"))
    assert res.status_code == 201, res.text
    detail = api.client.get(f"/v1/tickets/{res.json()['ticket_id']}", headers=api.auth("viewer")).json()
    assert detail["ticket"]["channel"] == "simulator"
    assert "#886" in detail["messages"][0]["body"] or "#887" in detail["messages"][0]["body"]


def test_simulator_takes_the_next_order_each_time_and_rejects_unknown_presets(api):
    first = api.client.post("/v1/simulator", json={"preset": "late_order"}, headers=api.auth("support")).json()
    second = api.client.post("/v1/simulator", json={"preset": "late_order"}, headers=api.auth("support")).json()
    bodies = [
        api.client.get(f"/v1/tickets/{t['ticket_id']}", headers=api.auth("viewer")).json()["messages"][0]["body"]
        for t in (first, second)
    ]
    assert bodies[0] != bodies[1]
    assert api.client.post("/v1/simulator", json={"preset": "anything"}, headers=api.auth("support")).status_code == 422
    assert api.client.post("/v1/simulator", json={"preset": "late_order"}, headers=api.auth("viewer")).status_code == 403


class FakeShopifyShop:
    """Only what the simulator asks the Shopify backend for: seeded test orders by kind."""

    def __init__(self, orders: dict[str, list[tuple[str, str]]]) -> None:
        self.orders = orders

    def find_test_orders(self, kind: str, limit: int = 10) -> list[tuple[str, str]]:
        return self.orders.get(kind, [])


def test_simulator_uses_shopify_test_orders_when_the_store_is_shopify(api, monkeypatch):
    monkeypatch.setattr(settings, "store_backend", "shopify")
    api.app.state.shop = FakeShopifyShop({"late_auto": [("#1005", "Ayumu.Hirano@example.com")]})
    res = api.client.post("/v1/simulator", json={"preset": "late_order"}, headers=api.auth("support"))
    assert res.status_code == 201, res.text
    detail = api.client.get(f"/v1/tickets/{res.json()['ticket_id']}", headers=api.auth("viewer")).json()
    assert detail["ticket"]["customer_email"] == "ayumu.hirano@example.com"
    assert "#1005" in detail["messages"][0]["body"]
    assert "#886" not in detail["messages"][0]["body"]  # never a MockShop order


def test_simulator_says_so_when_shopify_has_no_test_orders(api, monkeypatch):
    monkeypatch.setattr(settings, "store_backend", "shopify")
    api.app.state.shop = FakeShopifyShop({})
    res = api.client.post("/v1/simulator", json={"preset": "damaged_item"}, headers=api.auth("support"))
    assert res.status_code == 404 and res.json()["error"]["code"] == "NOT_SEEDED"


# ------------------------------------------------------------------ webhook
MAIL = {"from_email": "Ali@Example.com", "subject": "Late order", "body": "Where is my order #88601?"}


def test_webhook_refuses_without_the_right_secret(api, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    assert api.client.post("/v1/webhooks/email", json=MAIL).status_code == 401
    assert api.client.post("/v1/webhooks/email", json=MAIL, headers={"X-Webhook-Secret": "wrong"}).status_code == 401


def test_webhook_refuses_everything_when_no_secret_is_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "")
    res = api.client.post("/v1/webhooks/email", json=MAIL, headers={"X-Webhook-Secret": ""})
    assert res.status_code == 401


def test_webhook_creates_one_ticket_even_when_delivered_twice(api, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    headers = {"X-Webhook-Secret": "s3cret"}
    first = api.client.post("/v1/webhooks/email", json=MAIL, headers=headers)
    second = api.client.post("/v1/webhooks/email", json=MAIL, headers=headers)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["ticket_id"] == second.json()["ticket_id"]
    with api.sf() as s:
        assert s.scalars(select(TicketRow.id).where(TicketRow.channel == "email")).all() == [first.json()["ticket_id"]]


# ------------------------------------------------------------------ reports and feedback
def test_reports_need_a_manager_and_running_one_needs_an_admin(api):
    assert api.client.get("/v1/reports", headers=api.auth("support")).status_code == 403
    assert api.client.get("/v1/reports", headers=api.auth("manager")).json() == []
    assert api.client.post("/v1/admin/reports/run", headers=api.auth("manager")).status_code == 403
    assert api.client.get("/v1/reports/1/link", headers=api.auth("manager")).status_code == 404


def test_feedback_is_written_to_the_audit_log(api):
    ticket_id = new_ticket(api)
    res = api.client.post(
        "/v1/feedback", json={"ticket_id": ticket_id, "rating": "down", "note": "wrong tone"}, headers=api.auth("support")
    )
    assert res.status_code == 200 and res.json()["ok"] is True
    with api.sf() as s:
        row = s.scalars(select(AuditLogRow).where(AuditLogRow.event == "feedback")).one()
    assert row.detail_json["rating"] == "down" and row.detail_json["ticket_id"] == ticket_id

    missing = api.client.post("/v1/feedback", json={"ticket_id": "T-9999", "rating": "up"}, headers=api.auth("support"))
    assert missing.status_code == 404
