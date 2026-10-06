"""Step P tests: the Reports agent, the daily report job and the router path.

No Docker, no model, no internet: the seeded in-memory store from conftest.py. The model is replaced by a fake or made
to fail, so these tests are fast and always give the same answer.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from shoppilot.agents import reports as reports_module
from shoppilot.agents.reports import ReportText, build_reports_graph, check_text, plain_actions, plain_summary
from shoppilot.agents.supervisor import build_supervisor_graph, route_item
from shoppilot.core.config import settings
from shoppilot.db.models import MessageRow, ReportRunRow, TicketRow
from shoppilot.jobs import daily_report as job_module
from shoppilot.jobs.daily_report import render_html, run_daily_report

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")  # SQLite and Decimal

NOW = datetime(2026, 10, 1, 12, 0, 0)  # the same fixed clock as conftest.py

DAY = NOW.date().isoformat()  # 2026-10-01
FAKE_TEXT = ReportText(summary="A calm day with a few orders.", actions=["Reorder the low stock items"])

NUMBERS = {
    "day": DAY,
    "orders_count": 4,
    "sales_pkr": 12500,
    "refunds_count": 1,
    "refunds_pkr": 900,
    "late_orders": 2,
    "low_stock": [{"sku": "KURTA-M-BLK", "on_hand": 3, "reorder_point": 10}],
}


def count(env, model, *where) -> int:
    with env.sf() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


@pytest.fixture
def job(env, monkeypatch, tmp_path):
    """Settings for the job: no S3, a fake owner address, reports saved in a temp folder, a fake model."""
    monkeypatch.setattr(settings, "s3_bucket", "")
    monkeypatch.setattr(settings, "owner_email", "owner@example.com")
    monkeypatch.setattr(job_module, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(reports_module, "_ask_model", lambda numbers: FAKE_TEXT)
    return env


# ------------------------------------------------------------------ plain text (no model)
def test_plain_summary_and_actions_use_only_the_numbers():
    summary = plain_summary(NUMBERS)
    assert "4 orders" in summary and "12,500" in summary and "2 orders are late" in summary
    assert plain_actions(NUMBERS) == ["Reorder KURTA-M-BLK (3 on hand, reorder point 10)", "Ask the courier about the 2 late orders"]


def test_plain_actions_when_nothing_is_wrong():
    calm = {**NUMBERS, "late_orders": 0, "low_stock": []}
    assert plain_actions(calm) == ["No action needed today"]


def test_plain_actions_never_gives_more_than_three():
    many = {**NUMBERS, "low_stock": [{"sku": f"SKU-{n}", "on_hand": 1, "reorder_point": 5} for n in range(6)]}
    assert len(plain_actions(many)) == 3


@pytest.mark.parametrize(
    "bad",
    ["Write to evil@example.com", "Open http://evil.example/pay", "See www.evil.example", "<b>bold</b> summary text"],
    ids=["email", "link", "www", "html"],
)
def test_model_text_with_an_address_link_or_html_is_rejected(bad):
    assert check_text(ReportText(summary=bad + " today", actions=["Reorder stock"])) is False
    assert check_text(ReportText(summary="A calm day with a few orders.", actions=[bad])) is False


def test_clean_model_text_is_accepted():
    assert check_text(FAKE_TEXT) is True


# ------------------------------------------------------------------ the agent
def test_agent_uses_the_model_text_when_it_is_safe(env, monkeypatch):
    env.start("owner@example.com")
    monkeypatch.setattr(reports_module, "_ask_model", lambda numbers: FAKE_TEXT)
    out = build_reports_graph().invoke({"ticket_id": "T-1"})
    assert not out.get("errors")
    assert out["summary"] == FAKE_TEXT.summary and out["actions"] == FAKE_TEXT.actions
    assert out["numbers"]["day"] == DAY
    assert "Report for" in out["outcome"] and FAKE_TEXT.summary in out["outcome"]


def test_agent_falls_back_to_plain_text_when_the_model_fails(env, monkeypatch):
    env.start("owner@example.com")

    def broken(temperature: float = 0.0):
        raise RuntimeError("model is down")

    monkeypatch.setattr(reports_module, "get_llm", broken)
    out = build_reports_graph().invoke({"ticket_id": "T-1"})
    assert not out.get("errors")
    assert out["summary"] == plain_summary(out["numbers"])  # built by code from the same numbers
    assert out["actions"] == plain_actions(out["numbers"])


def test_agent_without_a_run_context_makes_no_report():
    out = build_reports_graph().invoke({"ticket_id": "T-1"})
    assert out["errors"] == ["CONFIG_ERROR"]
    assert out["outcome"].startswith("No report was made")


# ------------------------------------------------------------------ the HTML page
def test_html_escapes_every_text():
    page = render_html(NUMBERS, "<script>alert(1)</script> summary", ["<img src=x onerror=alert(1)>"])
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<img" not in page and "&lt;img" in page


def test_html_shows_the_numbers_and_low_stock():
    page = render_html(NUMBERS, "A calm day.", ["Reorder stock"])
    assert f"Daily report {DAY}" in page and "12,500" in page and "KURTA-M-BLK" in page


# ------------------------------------------------------------------ the daily report job
def test_job_saves_the_html_and_marks_the_run_done(job, tmp_path):
    result = run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    assert result["status"] == "done"
    saved = tmp_path / f"daily-{DAY}.html"
    assert saved.exists() and f"Daily report {DAY}" in saved.read_text(encoding="utf-8")
    with job.sf() as s:
        run = s.scalar(select(ReportRunRow))
    assert run.kind == "daily" and run.status == "done" and run.s3_key == f"reports/daily-{DAY}.html"


def test_job_queues_one_email_for_the_owner(job):
    run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    with job.sf() as s:
        ticket = s.get(TicketRow, f"report-{DAY}")
        message = s.scalar(select(MessageRow).where(MessageRow.ticket_id == f"report-{DAY}"))
    assert ticket.customer_email == "owner@example.com" and ticket.channel == "scheduler"
    assert message.direction == "outbound" and FAKE_TEXT.summary in message.body


def test_a_second_run_on_the_same_day_does_nothing(job):
    first = run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    again = run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    assert first["status"] == "done" and again["status"] == "skipped"
    assert count(job, ReportRunRow) == 1 and count(job, MessageRow) == 1  # no second row, no second email


def test_without_an_owner_email_the_report_is_saved_but_no_email_is_queued(job, monkeypatch):
    monkeypatch.setattr(settings, "owner_email", "")
    result = run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    assert result["status"] == "done"
    assert count(job, MessageRow) == 0


def test_a_failed_save_is_recorded_and_the_next_run_can_try_again(job, monkeypatch):
    real_save = job_module.save_report
    attempts = []

    def fails_once(page: str, key: str) -> str:
        attempts.append(key)
        if len(attempts) == 1:
            raise OSError("disk is full")
        return real_save(page, key)

    monkeypatch.setattr(job_module, "save_report", fails_once)
    with pytest.raises(OSError):
        run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)
    with job.sf() as s:
        assert s.scalar(select(ReportRunRow.status)) == "failed"
    assert count(job, MessageRow) == 0  # no email for a report that was not saved

    assert run_daily_report(sf=job.sf, shop=job.shop, now=lambda: NOW)["status"] == "done"  # the disk works again
    assert count(job, MessageRow) == 1


# ------------------------------------------------------------------ the router path
def test_the_daily_report_event_goes_to_the_reports_agent_without_a_model():
    routing = route_item("scheduled_event", "", event="daily_report")
    assert routing["route"] == "reports" and routing["by"] == "rule"


def test_a_customer_email_can_never_reach_the_reports_agent():
    routing = route_item("customer_email", "Please send me the sales report")
    assert routing["route"] == "support"


def test_supervisor_runs_the_reports_agent_for_the_scheduled_event(env, monkeypatch):
    ctx = env.start("owner@example.com")
    monkeypatch.setattr(reports_module, "_ask_model", lambda numbers: FAKE_TEXT)
    out = build_supervisor_graph().invoke(
        {"ticket_id": "T-1", "source": "scheduled_event", "text": "", "event": "daily_report"}
    )
    assert out["route"] == "reports" and not out.get("errors")
    assert out["outcome"].startswith("Report for")
    assert ctx.agent == ""  # acting_as put the agent name back
