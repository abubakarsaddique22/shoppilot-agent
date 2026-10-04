"""Policy knowledge base, search (Step G): the function behind the agent's search_policy tool (Step I).

It only works on Postgres (pgvector cosine distance). It never returns free text: every hit has its doc and section,
so the agent can cite it. If nothing is similar enough it returns NO_POLICY_FOUND, and the agent must escalate
instead of inventing a rule.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from shoppilot.core.config import settings
from shoppilot.db.models import PolicyChunkRow
from shoppilot.kb.embeddings import QueryEmbedder, embed_query

MAX_QUERY_CHARS = 500
MAX_K = 10


class PolicyHit(BaseModel):
    doc: str
    section: str  # heading path, e.g. "Returns > Damaged items"
    text: str
    version: str
    score: float  # cosine similarity, 1.0 is identical


class PolicySearchResult(BaseModel):
    ok: bool
    error: str | None = None  # EMPTY_QUERY | NO_POLICY_FOUND
    hits: list[PolicyHit] = Field(default_factory=list)


def search_policy(
    session: Session,
    query: str,
    k: int = 4,
    min_score: float | None = None,
    embed: QueryEmbedder = embed_query,
) -> PolicySearchResult:
    threshold = settings.kb_min_score if min_score is None else min_score
    question = query.strip()[:MAX_QUERY_CHARS]
    if not question:
        return PolicySearchResult(ok=False, error="EMPTY_QUERY")

    distance = PolicyChunkRow.embedding.cosine_distance(embed(question))  # type: ignore[attr-defined]
    rows = session.execute(
        select(PolicyChunkRow, distance.label("distance")).order_by(distance).limit(max(1, min(k, MAX_K)))
    ).all()
    hits = [
        PolicyHit(doc=row.doc, section=row.section, text=row.text, version=row.version, score=round(1 - dist, 4))
        for row, dist in rows
        if 1 - dist >= threshold
    ]
    if not hits:
        return PolicySearchResult(ok=False, error="NO_POLICY_FOUND")
    return PolicySearchResult(ok=True, hits=hits)
