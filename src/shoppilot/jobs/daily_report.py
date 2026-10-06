"""Daily report job (Step P): runs the Reports agent, saves an HTML report and queues the email to the owner.

    uv run python -m shoppilot.jobs.daily_report        # cron runs exactly this line (see docs/runbook.md)

Steps, in order:
  1. look in report_runs: if today's report is already done, stop (so a second cron run changes nothing)
  2. run the Reports agent (numbers, summary, up to 3 actions)
  3. render a small HTML page (every text is escaped) and save it: S3 when SHOP_S3_BUCKET is set, otherwise the local
     folder reports/ (so the job works on a laptop without AWS)
  4. write the owner's email as an outbound message on a ticket "report-YYYY-MM-DD" (queued, like the other emails;
     sending through SMTP or SES comes later) and mark the report_runs row done

The job uses the same graph path as a human request, with the fixed id report-YYYY-MM-DD.
"""
import html
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.agents.reports import build_reports_graph
from shoppilot.core.config import settings
from shoppilot.core.logging import get_logger
from shoppilot.db.models import MessageRow, ReportRunRow, TicketRow, utcnow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.mockshop import MockShop
from shoppilot.tools.context import RunContext, audit, ctx_var

log = get_logger(__name__)

REPORT_DIR = Path("reports")  # used when no S3 bucket is set

STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;color:#1b2430}"
    "h1{color:#1f3a5f}td,th{padding:6px 12px;border-bottom:1px solid #ddd;text-align:left}"
)


# ------------------------------------------------------------------------------------------------ helpers
def render_html(numbers: dict[str, Any], summary: str, actions: list[str]) -> str:
    """A small, static page. All text goes through html.escape: nothing in it can run as a script."""
    e = html.escape
    rows = [
        ("Orders placed", numbers["orders_count"]),
        ("Sales (PKR)", f"{numbers['sales_pkr']:,}"),
        ("Refunds", numbers["refunds_count"]),
        ("Refunded (PKR)", f"{numbers['refunds_pkr']:,}"),
        ("Late orders", numbers["late_orders"]),
    ]
    table = "".join(f"<tr><th scope='row'>{e(label)}</th><td>{e(str(value))}</td></tr>" for label, value in rows)
    low = (
        "".join(
            f"<li>{e(i['sku'])}: {i['on_hand']} on hand (reorder point {i['reorder_point']})</li>"
            for i in numbers["low_stock"]
        )
        or "<li>none</li>"
    )
    todo = "".join(f"<li>{e(a)}</li>" for a in actions)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>ShopPilot report {e(str(numbers['day']))}</title><style>{STYLE}</style></head><body>"
        f"<h1>Daily report {e(str(numbers['day']))}</h1><p>{e(summary)}</p>"
        f"<table>{table}</table><h2>Recommended actions</h2><ul>{todo}</ul>"
        f"<h2>Low stock</h2><ul>{low}</ul></body></html>"
    )


def save_report(page: str, key: str) -> str:
    """Save the page and return where it is. S3 when a bucket is set, otherwise a local file."""
    if settings.s3_bucket:
        import boto3  # only needed in production

        boto3.client("s3").put_object(
            Bucket=settings.s3_bucket, Key=key, Body=page.encode("utf-8"), ContentType="text/html; charset=utf-8"
        )
        return f"s3://{settings.s3_bucket}/{key}"
    path = REPORT_DIR / Path(key).name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return str(path)


def queue_owner_email(sf: sessionmaker[Session], thread_id: str, summary: str, actions: list[str], location: str) -> bool:
    """Write the email as an outbound message on the report ticket. Returns False when no owner email is set."""
    if not settings.owner_email:
        log.warning("reports: SHOP_OWNER_EMAIL is not set, no email was queued")
        return False
    body = "Hello,\n\n" + summary + "\n\nRecommended actions:\n" + "\n".join(f"- {a}" for a in actions)
    body += f"\n\nFull report: {location}\n\nRegards,\nShopPilot"
    with sf() as session:
        if session.get(TicketRow, thread_id) is None:
            session.add(
                TicketRow(
                    id=thread_id, channel="scheduler", customer_email=settings.owner_email,
                    subject="Daily report", status="done",
                )  # fmt: skip
            )
            session.flush()  # the ticket must exist before the message that points to it
        session.add(MessageRow(ticket_id=thread_id, direction="outbound", body=body))
        session.commit()
    return True


# ------------------------------------------------------------------------------------------------ the job
def run_daily_report(
    sf: sessionmaker[Session] | None = None,
    shop: MockShop | None = None,
    now: Callable[[], datetime] = utcnow,
) -> dict[str, Any]:
    """Make today's report once. Returns {"status": "done" | "skipped", "location": ..., "outcome": ...}."""
    sf = sf or make_session_factory(make_engine())
    shop = shop or MockShop(sf, now=now)
    day = now().date().isoformat()
    thread_id = f"report-{day}"
    key = f"reports/daily-{day}.html"

    with sf() as session:
        done = session.scalar(
            select(ReportRunRow).where(
                ReportRunRow.kind == "daily", ReportRunRow.s3_key == key, ReportRunRow.status == "done"
            )
        )
    if done is not None:
        return {"status": "skipped", "location": key, "outcome": f"The report for {day} already exists."}

    with sf() as session:
        run = ReportRunRow(kind="daily", s3_key=key, status="running")
        session.add(run)
        session.commit()
        run_id = run.id

    ctx = RunContext(
        shop=shop, session_factory=sf, ticket_id=thread_id, customer_email=settings.owner_email,
        actor_id="system", actor_role="system", now=now, agent="reports",
    )  # fmt: skip
    token = ctx_var.set(ctx)
    try:
        out = build_reports_graph().invoke({"ticket_id": thread_id})
        if out.get("errors"):
            raise RuntimeError(f"the Reports agent failed: {out['errors']}")
        page = render_html(out["numbers"], out["summary"], out["actions"])
        location = save_report(page, key)
        queue_owner_email(sf, thread_id, out["summary"], out["actions"], location)
        audit("report_created", kind="daily", location=location)
    except Exception:
        _set_status(sf, run_id, "failed")
        log.exception("daily report failed")
        raise
    finally:
        ctx_var.reset(token)

    _set_status(sf, run_id, "done")
    return {"status": "done", "location": location, "outcome": out["outcome"]}


def _set_status(sf: sessionmaker[Session], run_id: int, status: str) -> None:
    with sf() as session:
        row = session.get(ReportRunRow, run_id)
        if row is not None:
            row.status = status
            session.commit()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()  # the LangSmith SDK reads LANGSMITH_* from the environment
    print(run_daily_report())
