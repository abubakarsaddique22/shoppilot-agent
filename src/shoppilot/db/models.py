"""Application tables (Step H): users, tickets, messages, approvals, actions, audit_log, policy_chunks, report_runs.

These tables belong to ShopPilot itself and are managed by Alembic. They use their own metadata (`AppBase`),
separate from the MockShop tables (`MockBase`, Step E) which Shopify replaces in production.
LangGraph checkpoint tables are created later by the checkpointer setup call (Step N); do not model them here.

Works on Postgres and SQLite (unit tests). Datetimes are naive UTC, like the rest of the project.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite only auto-numbers a plain INTEGER primary key.
BigId = BigInteger().with_variant(Integer(), "sqlite")
# JSONB on Postgres, plain JSON on SQLite.
JsonType = JSON().with_variant(JSONB(), "postgresql")
# fastembed default model (BAAI/bge-small-en-v1.5) gives 384 numbers per chunk. On SQLite the vector is stored as JSON.
EMBEDDING_DIM = 384
VectorType = Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AppBase(DeclarativeBase):
    pass


class UserRow(AppBase):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'support', 'manager', 'owner', 'admin')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TicketRow(AppBase):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'working', 'waiting_approval', 'done', 'escalated')", name="ck_tickets_status"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)  # "T-1042"; also the LangGraph thread_id
    channel: Mapped[str] = mapped_column(String, default="ui")  # ui | email | scheduler | simulator
    customer_email: Mapped[str] = mapped_column(String, index=True)
    subject: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    intent: Mapped[str | None] = mapped_column(String, nullable=True)
    order_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_pkr: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MessageRow(AppBase):
    __tablename__ = "messages"
    __table_args__ = (CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_messages_direction"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    direction: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)  # untrusted text: never render it as HTML
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApprovalRow(AppBase):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("tier IN ('manager', 'owner')", name="ck_approvals_tier"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired')", name="ck_approvals_status"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    action: Mapped[str] = mapped_column(String)  # "refund"
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonType)  # amount, reasons, evidence summary
    tier: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActionRow(AppBase):
    """One row per side effect. The unique idempotency_key makes a repeated write safe."""

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    tool: Mapped[str] = mapped_column(String)
    args_json: Mapped[dict[str, Any]] = mapped_column(JsonType)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)  # e.g. "T-1042:O-88731:refund"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLogRow(AppBase):
    """Append-only. The migration adds a Postgres trigger that rejects UPDATE and DELETE."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String)
    event: Mapped[str] = mapped_column(String)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PolicyChunkRow(AppBase):
    """Policy knowledge base for search_policy (Step G). `section` holds the heading path, e.g. "Returns > Damaged items"."""

    __tablename__ = "policy_chunks"
    __table_args__ = (
        Index(
            "ix_policy_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    doc: Mapped[str] = mapped_column(String, index=True)
    section: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String, default="1")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(VectorType)


class ReportRunRow(AppBase):
    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)  # daily | low_stock
    s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String, default="running")  # running | done | failed
