"""Text embeddings for the policy knowledge base (Step G). fastembed runs on CPU through ONNX, no API key and no torch.

The model is downloaded once (a few hundred MB) into .cache/fastembed in the project folder.
Its output size must match EMBEDDING_DIM in shoppilot.db.models (384 for BAAI/bge-small-en-v1.5).
Change the model with SHOP_EMBEDDING_MODEL and run scripts/ingest_policies.py again.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from shoppilot.core.config import settings
from shoppilot.db.models import EMBEDDING_DIM

if TYPE_CHECKING:
    from fastembed import TextEmbedding

# Tests pass a fake embedder with the same shape, so they never download the model.
PassageEmbedder = Callable[[Sequence[str]], list[list[float]]]
QueryEmbedder = Callable[[str], list[float]]

# src/shoppilot/kb/embeddings.py -> repo root is three levels up
CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "fastembed"


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    # imported here, not at the top, so unit tests with a fake embedder never load the model library
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=settings.embedding_model, cache_dir=str(CACHE_DIR))


def _check(vector: list[float]) -> list[float]:
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"model {settings.embedding_model} gives {len(vector)} numbers, but the database column expects {EMBEDDING_DIM}"
        )
    return vector


def embed_passages(texts: Sequence[str]) -> list[list[float]]:
    """Embed policy chunks (documents)."""
    return [_check(v.tolist()) for v in _model().passage_embed(list(texts))]


def embed_query(query: str) -> list[float]:
    """Embed a search question."""
    return _check(next(iter(_model().query_embed(query))).tolist())
