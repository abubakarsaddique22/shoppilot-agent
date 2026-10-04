"""Policy knowledge base, ingestion (Step G): configs/policies/*.md -> chunks -> embeddings -> policy_chunks table.

One chunk per section. The heading path ("Returns > Damaged items") is written at the top of every chunk, so the
embedding knows which document and section the text belongs to, and the agent can cite it.
Only level-2 and deeper headings are indexed: the text under the "# Title" line is an intro, not a policy.
Running the ingestion again replaces the rows of each document, so it is safe to repeat.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from shoppilot.db.models import PolicyChunkRow
from shoppilot.kb.embeddings import PassageEmbedder, embed_passages

# src/shoppilot/kb/ingest.py -> repo root is three levels up
POLICY_DIR = Path(__file__).resolve().parents[3] / "configs" / "policies"
MAX_CHARS = 1800  # about 450 tokens; longer sections are split further
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


class Chunk(BaseModel):
    doc: str  # file name without .md, e.g. "returns"
    section: str  # heading path, e.g. "Returns > Damaged items"
    text: str  # heading path + body: this is what gets embedded and returned


def chunk_markdown(doc: str, markdown: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []  # (heading level, title) from the top heading down to the current one
    body: list[str] = []
    splitter = RecursiveCharacterTextSplitter(chunk_size=max_chars, chunk_overlap=min(100, max_chars // 5))

    def flush() -> None:
        text = "\n".join(body).strip()
        body.clear()
        if not stack or stack[-1][0] < 2 or not text:
            return
        section = " > ".join(title for _, title in stack)
        pieces = splitter.split_text(text) if len(text) > max_chars else [text]
        chunks.extend(Chunk(doc=doc, section=section, text=f"{section}\n\n{piece}") for piece in pieces)

    for line in markdown.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2)))
        else:
            body.append(line)
    flush()
    return chunks


def ingest_policies(
    session_factory: sessionmaker[Session],
    policy_dir: Path | str = POLICY_DIR,
    embed: PassageEmbedder = embed_passages,
) -> dict[str, int]:
    """Replace the stored chunks of every policy file. Returns {doc: number of chunks}."""
    files = sorted(Path(policy_dir).glob("*.md"))
    if not files:
        raise FileNotFoundError(f"no policy .md files in {policy_dir}")  # never wipe the table by accident
    counts: dict[str, int] = {}
    with session_factory() as session:
        for path in files:
            markdown = path.read_text(encoding="utf-8")
            chunks = chunk_markdown(path.stem, markdown)
            vectors = embed([c.text for c in chunks]) if chunks else []  # embed first: a failure keeps the old rows
            version = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:8]
            effective = date.fromtimestamp(path.stat().st_mtime)
            session.execute(delete(PolicyChunkRow).where(PolicyChunkRow.doc == path.stem))
            session.add_all(
                PolicyChunkRow(
                    doc=c.doc, section=c.section, version=version, effective_date=effective, text=c.text, embedding=v
                )
                for c, v in zip(chunks, vectors, strict=True)
            )
            counts[path.stem] = len(chunks)
        # a policy file that was deleted must not stay searchable
        session.execute(delete(PolicyChunkRow).where(PolicyChunkRow.doc.not_in(list(counts))))
        session.commit()
    return counts
