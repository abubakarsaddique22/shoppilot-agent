"""Run context and shared helpers for the tools (Step I).

The model only passes small arguments (order_id, amount). Everything that decides WHO is asking lives here:
the ticket, the customer's email, the user's role. The API layer fills it from the signed JWT and the ticket,
never from model output, so a prompt injection cannot change it.

    token = ctx_var.set(RunContext(...))
    try:
        ...run the graph...
    finally:
        ctx_var.reset(token)

Helpers used by every tool:
- tool_guard: counts the call against the budget, checks the role, turns AppError into {"ok": False, "error": ...}
- find_action / record_action: idempotency through the actions table (unique idempotency_key)
- audit: one row in audit_log
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.core.config import settings
from shoppilot.core.errors import AppError, BudgetExceeded, ConfigError, PermissionDenied
from shoppilot.core.logging import get_logger
from shoppilot.db.models import ActionRow, AuditLogRow, utcnow
from shoppilot.policy.limits import Limits
from shoppilot.shop.base import ShopBackend

log = get_logger(__name__)

MAX_WRITES = 2  # Table 13: 6 reads (settings.max_tool_calls) and 2 writes per ticket
WRITE_ROLES = {"support", "manager", "owner", "admin", "system"}  # a viewer can read but never write

# Step O: every agent has its own tool allow-list. The tool layer enforces it, whatever the model asks for.
# escalate_to_human is the safe exit, so every agent has it. sales_summary is built with the Reports agent (Step P).
AGENT_TOOLS: dict[str, frozenset[str]] = {
    "support": frozenset(
        {
            "get_order",
            "find_orders_by_email",
            "track_shipment",
            "search_policy",
            "propose_refund",
            "issue_refund",
            "send_customer_email",
            "escalate_to_human",
        }
    ),
    "inventory": frozenset({"get_inventory", "get_product", "create_purchase_order_draft", "escalate_to_human"}),
    "listing": frozenset({"get_product", "create_product_draft", "escalate_to_human"}),
    "reports": frozenset({"sales_summary", "get_inventory", "get_product", "escalate_to_human"}),
}


@dataclass
class RunContext:
    shop: ShopBackend
    session_factory: sessionmaker[Session]
    ticket_id: str
    customer_email: str  # the customer who wrote the ticket; tools only show this person's orders
    actor_id: str  # user id or "system"
    actor_role: str  # viewer | support | manager | owner | admin | system
    limits: Limits = field(default_factory=Limits)
    now: Callable[[], datetime] = utcnow  # tests pass a fixed clock
    reads_used: int = 0
    writes_used: int = 0
    agent: str = ""  # support | inventory | listing | reports. "" means no allow-list (tests, scripts)


ctx_var: ContextVar[RunContext] = ContextVar("run_ctx")


def get_ctx() -> RunContext:
    try:
        return ctx_var.get()
    except LookupError:
        raise ConfigError("tool called without a run context (ctx_var was not set)") from None


def use_call_budget(kind: Literal["read", "write"]) -> RunContext:
    """Count one tool call. Raises BudgetExceeded when the ticket has used up its calls."""
    ctx = get_ctx()
    if kind == "read":
        if ctx.reads_used >= settings.max_tool_calls:
            raise BudgetExceeded(f"read tool limit reached ({settings.max_tool_calls} per ticket)")
        ctx.reads_used += 1
    else:
        if ctx.writes_used >= MAX_WRITES:
            raise BudgetExceeded(f"write tool limit reached ({MAX_WRITES} per ticket)")
        ctx.writes_used += 1
    return ctx


def _check_agent_allowed(tool_name: str) -> None:
    """Raise FORBIDDEN when the running agent does not have this tool. Checked before the budget is used."""
    ctx = ctx_var.get(None)
    if ctx is not None and ctx.agent and tool_name not in AGENT_TOOLS.get(ctx.agent, frozenset()):
        raise PermissionDenied(f"the {ctx.agent} agent may not use {tool_name}")


def tool_guard(kind: Literal["read", "write"] | None = "read") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Put under @tool. kind=None means no budget and no role check (escalate_to_human, the safe exit)."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                _check_agent_allowed(fn.__name__)
                if kind == "write" and get_ctx().actor_role not in WRITE_ROLES:
                    raise PermissionDenied(f"role {get_ctx().actor_role} cannot use write tools")
                if kind is not None:
                    use_call_budget(kind)
                return fn(*args, **kwargs)
            except AppError as err:
                log.warning("tool %s failed: %s", fn.__name__, err.code, extra={"tool": fn.__name__, "code": err.code})
                return err.to_result()

        return wrapper

    return decorator


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def find_action(key: str) -> dict[str, Any] | None:
    """The earlier result for this idempotency key, or None when the action was never done."""
    ctx = get_ctx()
    with ctx.session_factory() as session:
        row = session.scalar(select(ActionRow).where(ActionRow.idempotency_key == key))
        if row is None:
            return None
        return row.result_json or {"ok": True}


def record_action(tool: str, args: dict[str, Any], result: dict[str, Any], key: str) -> None:
    """One row per side effect. If the same key was recorded a moment ago, the earlier row wins."""
    ctx = get_ctx()
    try:
        with ctx.session_factory() as session:
            session.add(
                ActionRow(
                    ticket_id=ctx.ticket_id,
                    tool=tool,
                    args_json=_jsonable(args),
                    result_json=_jsonable(result),
                    idempotency_key=key,
                )
            )
            session.commit()
    except IntegrityError:
        pass


def audit(event: str, **detail: Any) -> None:
    """Add one row to audit_log (append-only). Keep it small; never put secrets here."""
    ctx = get_ctx()
    with ctx.session_factory() as session:
        session.add(
            AuditLogRow(actor=ctx.actor_id, event=event, detail_json=_jsonable({"ticket_id": ctx.ticket_id, **detail}))
        )
        session.commit()
