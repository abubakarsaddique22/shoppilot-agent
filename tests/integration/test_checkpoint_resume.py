"""Step N "done when": a run that waits for a human survives a process restart.

Needs Postgres (docker compose up -d db). The test makes its own throw-away database (shoppilot_ckpt_test), seeds it,
and drops it at the end, so your dev data is never touched. No model call: the run is put right after the rules node,
and the approval gate, the approvals service, execute, verify and the Postgres checkpointer are all real.

Each worker call is a separate process (tests/integration/_ckpt_worker.py). The first one dies at the approval gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from shoppilot.agents.checkpoint import close_checkpointer, open_checkpointer, prune_checkpoints
from shoppilot.core.config import settings
from shoppilot.db.models import AppBase
from shoppilot.shop.seed import seed_database

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("_ckpt_worker.py")
TEST_DB = "shoppilot_ckpt_test"
NOW = datetime(2026, 10, 1, 12, 0, 0)  # the clock the seed and the worker use


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    base = make_url(settings.database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("select 1"))
    except OperationalError:
        admin.dispose()
        pytest.skip("Postgres is not running (docker compose up -d db)")

    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    url = base.set(database=TEST_DB)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    seed_database(engine, NOW)
    AppBase.metadata.create_all(engine)
    engine.dispose()

    yield url.render_as_string(hide_password=False)

    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    admin.dispose()


def run_worker(url: str, *args: str) -> dict[str, Any]:
    """Run one helper process against the test database and return its RESULT line."""
    env = {
        **os.environ,
        "SHOP_DATABASE_URL": url,
        "LANGSMITH_TRACING": "false",
        "SHOP_LANGSMITH_TRACING": "false",
        "PYTHONPATH": str(ROOT / "src"),
    }
    done = subprocess.run(
        [sys.executable, str(WORKER), *args],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=180, check=False,
    )  # fmt: skip
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"worker {args} printed no result\nstdout: {done.stdout[-1500:]}\nstderr: {done.stderr[-1500:]}"
    return json.loads(lines[-1][len("RESULT "):])


@pytest.fixture(scope="module")
def started(db_url: str) -> dict[str, Any]:
    """The first process: runs to the approval gate and dies there."""
    return run_worker(db_url, "start")


def test_the_run_stops_at_the_approval_gate_and_moves_no_money(started: dict[str, Any]) -> None:
    snap = started["snapshot"]
    assert snap["next_nodes"] == ["approval_gate"] and not snap["finished"]
    assert snap["pending_interrupt"]["tier"] == "manager" and snap["pending_interrupt"]["amount_pkr"] == 500
    assert snap["pending_interrupt"]["approval_id"] == started["approval_id"]
    assert started["refunded_total"] == started["refunded_before"]


def test_after_a_restart_the_run_resumes_and_refunds_exactly_once(db_url: str, started: dict[str, Any]) -> None:
    paused = run_worker(db_url, "status")  # a brand new process finds the run where the old one died
    assert paused["snapshot"]["next_nodes"] == ["approval_gate"]
    assert paused["approval_id"] == started["approval_id"]
    assert paused["refunded_total"] == started["refunded_before"]

    done = run_worker(db_url, "resume", str(started["approval_id"]))  # another new process: approve and resume
    snap = done["snapshot"]
    assert snap["finished"] and snap["verified"] and not snap["escalated"]
    assert snap["approval_status"] == "approved" and snap["errors"] == []
    assert done["refunded_total"] == started["refunded_before"] + 500

    again = run_worker(db_url, "status")
    assert again["snapshot"]["finished"] and again["refunded_total"] == done["refunded_total"]


def test_old_checkpoints_are_pruned_and_new_ones_stay(db_url: str, started: dict[str, Any]) -> None:
    saver = open_checkpointer(db_url.replace("postgresql+psycopg://", "postgresql://"))
    try:
        assert prune_checkpoints(saver, days=30) == 0  # the thread is new
        assert prune_checkpoints(saver, days=30, now=datetime.now(UTC) + timedelta(days=31)) == 1
    finally:
        close_checkpointer(saver)
    assert run_worker(db_url, "status")["snapshot"]["exists"] is False
