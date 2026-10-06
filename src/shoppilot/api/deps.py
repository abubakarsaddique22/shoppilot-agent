"""Shared FastAPI dependencies (Step Q): who is asking, the database, the shop, the graph and the run context.

Rules that every router follows:
- The user comes ONLY from the signed JWT (`current_user`). The role in the token is signed, so the client cannot change it.
- A router never builds a RunContext by hand: `build_run_context` does it from the token and the ticket row, never from
  the request body and never from model output (blueprint Step S). The tools read the role from that context.
- Everything the app needs lives on `app.state` (filled once in the lifespan, see api/main.py): session_factory, shop,
  limits, graph, and an optional clock. Tests put fakes there. A missing piece gives a clean 503, not a crash.

Who may do what (the table in blueprint section 11, with the viewer role "reads only"):

    viewer   read tickets
    support  + create tickets, run the agent, use the simulator
    manager  + approval inbox, decide manager-tier approvals, reports
    owner    + decide owner-tier approvals
    admin    + admin endpoints. Admin can NOT decide an approval: a technical role must not move money.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.api.schemas import UserOut
from shoppilot.core.errors import AuthError, DatabaseError, PermissionDenied
from shoppilot.core.security import ROLES, decode_token
from shoppilot.db.models import AuditLogRow, utcnow
from shoppilot.policy.limits import Limits
from shoppilot.shop.base import ShopBackend
from shoppilot.tools.context import RunContext

Clock = Callable[[], datetime]

ALL_ROLES = frozenset(ROLES)
SUPPORT_ROLES = frozenset({"support", "manager", "owner", "admin"})
MANAGER_ROLES = frozenset({"manager", "owner", "admin"})
ADMIN_ROLES = frozenset({"admin"})
# Who may DECIDE an approval, by tier. The approvals service and issue_refund check the tier again.
APPROVER_ROLES: dict[str, frozenset[str]] = {
    "manager": frozenset({"manager", "owner"}),
    "owner": frozenset({"owner"}),
}


def visible_tiers(role: str) -> list[str]:
    """The approval tiers a role may SEE in the inbox. Admin sees all of them (read only), everyone else what they can decide."""
    if role == "admin":
        return list(APPROVER_ROLES)
    return [tier for tier, roles in APPROVER_ROLES.items() if role in roles]


# ------------------------------------------------------------------ who is asking
_bearer = HTTPBearer(auto_error=False)  # we raise our own AuthError, so the JSON error shape stays the same everywhere


def current_user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> UserOut:
    """The logged-in user, read from the signed token. No database call: the role in the token is signed."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError("login required")
    claims = decode_token(credentials.credentials)  # AuthError (401) for an expired, forged or malformed token
    return UserOut(id=str(claims["sub"]), email=str(claims.get("email", "")), role=str(claims["role"]))


def require_role(*roles: str) -> Callable[..., UserOut]:
    """A dependency that lets only these roles through (403 for everybody else)."""
    allowed = frozenset(roles)

    def dependency(user: Annotated[UserOut, Depends(current_user)]) -> UserOut:
        if user.role not in allowed:
            raise PermissionDenied(f"role {user.role} may not do this")
        return user

    return dependency


# ------------------------------------------------------------------ what the app holds (app.state)
def get_session_factory(request: Request) -> sessionmaker[Session]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise DatabaseError("the database is not available", code="DATABASE_UNAVAILABLE")
    return factory


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]


def get_db(factory: SessionFactoryDep) -> Iterator[Session]:
    """One session per request. Routers that write call session.commit() themselves."""
    with factory() as session:
        yield session


def get_shop(request: Request) -> ShopBackend:
    shop = getattr(request.app.state, "shop", None)
    if shop is None:
        raise DatabaseError("the shop backend is not available", code="SHOP_UNAVAILABLE")
    return shop


def get_limits(request: Request) -> Limits:
    return getattr(request.app.state, "limits", None) or Limits()


def get_clock(request: Request) -> Clock:
    """The clock of the app. Production uses utcnow. Tests put a fixed clock on app.state, like the graph tests do."""
    return getattr(request.app.state, "clock", None) or utcnow


def get_graph_or_none(request: Request) -> Any:
    """The compiled supervisor graph, or None when the checkpointer could not start (the database was down)."""
    return getattr(request.app.state, "graph", None)


def get_graph(request: Request) -> Any:
    graph = get_graph_or_none(request)
    if graph is None:
        raise DatabaseError("the agent is not available: the database is down", code="AGENT_UNAVAILABLE")
    return graph


DbDep = Annotated[Session, Depends(get_db)]
ShopDep = Annotated[ShopBackend, Depends(get_shop)]
LimitsDep = Annotated[Limits, Depends(get_limits)]
ClockDep = Annotated[Clock, Depends(get_clock)]
GraphDep = Annotated[Any, Depends(get_graph)]
OptionalGraphDep = Annotated[Any, Depends(get_graph_or_none)]

ReaderUser = Annotated[UserOut, Depends(require_role(*sorted(ALL_ROLES)))]
SupportUser = Annotated[UserOut, Depends(require_role(*sorted(SUPPORT_ROLES)))]
ManagerUser = Annotated[UserOut, Depends(require_role(*sorted(MANAGER_ROLES)))]
AdminUser = Annotated[UserOut, Depends(require_role(*sorted(ADMIN_ROLES)))]


# ------------------------------------------------------------------ run context and audit
def build_run_context(
    *,
    shop: ShopBackend,
    factory: sessionmaker[Session],
    ticket_id: str,
    customer_email: str,
    user: UserOut,
    limits: Limits,
    clock: Clock,
) -> RunContext:
    """Who is asking and on which ticket, for the tool layer. Filled from the token and the ticket row only."""
    return RunContext(
        shop=shop,
        session_factory=factory,
        ticket_id=ticket_id,
        customer_email=customer_email,
        actor_id=user.id,
        actor_role=user.role,
        limits=limits,
        now=clock,
    )


def write_audit(factory: sessionmaker[Session], actor: str, event: str, **detail: Any) -> None:
    """One row in audit_log for an API action that has no run context (login, ticket created, approval decided).
    Keep the detail small and never put a password, a token or a customer message in it."""
    with factory() as session:
        session.add(AuditLogRow(actor=actor, event=event, detail_json=detail))
        session.commit()
