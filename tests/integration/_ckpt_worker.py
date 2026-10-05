"""Helper process for test_checkpoint_resume.py. It is not a test (the name does not start with test_).

Every call is a NEW process, so nothing survives in memory between calls. Only Postgres keeps the run.

    python _ckpt_worker.py start            refund of PKR 500 on a cash-on-delivery order, runs up to the approval gate,
                                            then the process dies at once (os._exit), like a crash or a restart
    python _ckpt_worker.py status           read the saved state, change nothing
    python _ckpt_worker.py resume <id>      a manager approves, then the graph resumes and finishes

The database comes from SHOP_DATABASE_URL (the test sets it to a throw-away database). It prints one line: RESULT {json}.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy import select

from shoppilot.agents.checkpoint import get_ticket_state, open_checkpointer
from shoppilot.agents.support import build_support_graph, rules
from shoppilot.approvals.service import record_decision
from shoppilot.db.models import ApprovalRow, TicketRow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.mock_models import OrderRow
from shoppilot.shop.mockshop import MockShop
from shoppilot.tools.context import RunContext, ctx_var

NOW = datetime(2026, 10, 1, 12, 0, 0)  # the same fixed clock as the seed
THREAD = "T-ckpt-1"
CONFIG = {"configurable": {"thread_id": THREAD}}


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("start", "status", "resume"):
        raise SystemExit("usage: _ckpt_worker.py start | status | resume <approval_id>")

    engine = make_engine()
    sf = make_session_factory(engine)
    shop = MockShop(sf, now=lambda: NOW)
    with sf() as s:
        name = s.scalars(select(OrderRow.name).where(OrderRow.scenario == "cod_refund").order_by(OrderRow.id)).first()
        if name is None:
            raise SystemExit("the seed has no cod_refund order")
        order = shop.get_order(name)
        if s.get(TicketRow, THREAD) is None:
            s.add(TicketRow(id=THREAD, customer_email=order.customer_email))
            s.commit()

    # the run context is what the API layer fills from the signed JWT: who is asking, which shop, which ticket
    ctx_var.set(
        RunContext(
            shop=shop, session_factory=sf, ticket_id=THREAD, customer_email=order.customer_email,
            actor_id="u1", actor_role="support", now=lambda: NOW,
        )  # fmt: skip
    )
    saver = open_checkpointer()
    graph = build_support_graph(saver)
    result: dict[str, Any] = {"refunded_before": shop.get_order(order.id).refunded_total}

    if command == "start":
        state: dict[str, Any] = {
            "ticket_id": THREAD,
            "intent": "refund",
            "order_ref": order.id,
            "order": {"id": order.id},
            "proposal": {
                "action": "refund",
                "amount_pkr": 500,
                "reason": "late",
                "evidence_ids": ["order:x"],
                "summary": "Order is late.",
            },
            "messages": [HumanMessage("refund please")],
            "actions_taken": [],
            "errors": [],
        }
        state["ruling"] = rules(state)["ruling"]  # the real policy engine, as after the rules node
        graph.update_state(CONFIG, state, as_node="rules")  # the run now stands right after the rules node
        graph.invoke(None, CONFIG)  # approval_gate runs and calls interrupt()
    elif command == "resume":
        record_decision(
            sf, int(argv[2]), status="approved", decided_by="manager@demo", note="Courier confirmed the delay", now=NOW
        )
        graph.invoke(Command(resume={"status": "approved"}), CONFIG)

    with sf() as s:
        approval = s.scalars(select(ApprovalRow).where(ApprovalRow.ticket_id == THREAD)).first()
        approval_id = approval.id if approval else None
    result["approval_id"] = approval_id
    result["refunded_total"] = shop.get_order(order.id).refunded_total
    result["snapshot"] = get_ticket_state(graph, THREAD).model_dump(mode="json")
    print("RESULT " + json.dumps(result, default=str), flush=True)

    if command == "start":
        os._exit(0)  # no clean shutdown: the process just disappears at the approval gate


if __name__ == "__main__":
    main(sys.argv)
