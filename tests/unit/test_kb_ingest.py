"""Step G: chunking and ingestion. No Docker and no model download: a fake embedder and in-memory SQLite."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.db.models import EMBEDDING_DIM, AppBase, PolicyChunkRow
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.kb.ingest import POLICY_DIR, chunk_markdown, ingest_policies
from shoppilot.policy import refund_rules

SAMPLE = """# Returns

Intro text that is not a policy.

## Refund window

You have 14 days.

## Damaged items

Send a photo.

### Evidence

A photo or a short description.
"""


def fake_embed(texts: Sequence[str]) -> list[list[float]]:
    return [[0.1] * EMBEDDING_DIM for _ in texts]


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = make_engine("sqlite://")
    AppBase.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def test_sections_get_a_heading_path() -> None:
    sections = [c.section for c in chunk_markdown("returns", SAMPLE)]
    assert sections == ["Returns > Refund window", "Returns > Damaged items", "Returns > Damaged items > Evidence"]


def test_intro_under_the_title_is_not_indexed() -> None:
    assert all("Intro text" not in c.text for c in chunk_markdown("returns", SAMPLE))


def test_every_chunk_starts_with_its_heading_path() -> None:
    for c in chunk_markdown("returns", SAMPLE):
        assert c.text.startswith(c.section + "\n\n")


def test_long_section_is_split_and_keeps_its_heading_path() -> None:
    markdown = "# Doc\n\n## Long\n\n" + " ".join(f"word{i}" for i in range(200))
    chunks = chunk_markdown("doc", markdown, max_chars=100)
    assert len(chunks) > 1
    assert all(c.section == "Doc > Long" and c.text.startswith("Doc > Long\n\n") for c in chunks)


def test_every_section_the_policy_engine_cites_exists_in_the_knowledge_base() -> None:
    sections = {c.section for p in POLICY_DIR.glob("*.md") for c in chunk_markdown(p.stem, p.read_text("utf-8"))}
    refs = {v for name, v in vars(refund_rules).items() if name.startswith("REF_")}
    assert refs, "no REF_ constants found"
    assert refs <= sections, f"policy engine cites sections that do not exist: {refs - sections}"


def test_ingest_fills_the_table(factory: sessionmaker[Session]) -> None:
    counts = ingest_policies(factory, embed=fake_embed)
    assert set(counts) == {"returns", "shipping", "exchange"}
    with factory() as s:
        rows = s.scalars(select(PolicyChunkRow)).all()
    assert len(rows) == sum(counts.values()) > 0
    assert all(len(r.embedding) == EMBEDDING_DIM and len(r.version) == 8 for r in rows)


def test_ingest_twice_does_not_duplicate(factory: sessionmaker[Session]) -> None:
    first = ingest_policies(factory, embed=fake_embed)
    second = ingest_policies(factory, embed=fake_embed)
    assert first == second
    with factory() as s:
        assert len(s.scalars(select(PolicyChunkRow)).all()) == sum(first.values())


def test_rows_of_deleted_documents_are_removed(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.add(PolicyChunkRow(doc="old", section="Old > Gone", text="x", embedding=[0.0] * EMBEDDING_DIM))
        s.commit()
    ingest_policies(factory, embed=fake_embed)
    with factory() as s:
        assert s.scalars(select(PolicyChunkRow).where(PolicyChunkRow.doc == "old")).first() is None


def test_empty_policy_folder_is_an_error(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ingest_policies(factory, policy_dir=tmp_path, embed=fake_embed)
