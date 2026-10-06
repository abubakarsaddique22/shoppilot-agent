"""Reports (Step Q): the list of generated reports, a short-lived download link, and "run the daily report now" (admin).

    GET  /v1/reports                  manager and above: newest first
    GET  /v1/reports/{id}/link        manager and above: a signed S3 link that works for 5 minutes
    POST /v1/admin/reports/run        admin: make today's report now (a second call on the same day changes nothing)

The bucket stays private (Block Public Access). The browser never gets the key of another object: the link is made
from the s3_key stored in report_runs, and only for a report that finished.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from shoppilot.api.deps import AdminUser, ClockDep, DbDep, ManagerUser, SessionFactoryDep, ShopDep, write_audit
from shoppilot.api.schemas import ReportOut, ReportRunOut
from shoppilot.core.config import settings
from shoppilot.core.errors import NotFound
from shoppilot.core.logging import get_logger
from shoppilot.db.models import ReportRunRow
from shoppilot.jobs.daily_report import run_daily_report

log = get_logger(__name__)
router = APIRouter(tags=["reports"])

LINK_SECONDS = 300


@router.get("/v1/reports")
def list_reports(
    user: ManagerUser,
    db: DbDep,
    kind: Annotated[Literal["daily", "low_stock"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReportOut]:
    stmt = select(ReportRunRow).order_by(ReportRunRow.created_at.desc(), ReportRunRow.id.desc())
    if kind:
        stmt = stmt.where(ReportRunRow.kind == kind)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [ReportOut.model_validate(row) for row in rows]


@router.get("/v1/reports/{report_id}/link")
def report_link(report_id: int, user: ManagerUser, db: DbDep) -> dict[str, str | int]:
    row = db.get(ReportRunRow, report_id)
    if row is None or row.status != "done" or not row.s3_key:
        raise NotFound(f"report {report_id} not found", code="REPORT_NOT_FOUND")
    if not settings.s3_bucket:
        raise NotFound("reports are saved on the server disk in this setup, there is no download link", code="REPORT_NOT_IN_S3")
    import boto3  # only needed when a bucket is configured (production)

    url = boto3.client("s3").generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": row.s3_key}, ExpiresIn=LINK_SECONDS
    )
    return {"url": url, "expires_in": LINK_SECONDS}


@router.post("/v1/admin/reports/run")
def run_report_now(user: AdminUser, factory: SessionFactoryDep, shop: ShopDep, clock: ClockDep) -> ReportRunOut:
    """Plain `def`: the report runs the Reports agent (a model call), so FastAPI gives it its own worker thread."""
    result = run_daily_report(factory, shop, now=clock)
    write_audit(factory, user.id, "report_run_requested", status=result["status"])
    return ReportRunOut(status=result["status"], location=result["location"], outcome=result["outcome"])
