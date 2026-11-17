"""Second-stage re-ranking with a cross-encoder.

First-stage retrieval (bi-encoder vectors + full-text) scores the query and each passage
*independently*, which is what makes it fast enough to search the whole collection. A
cross-encoder reads the query and a passage *together* and is far more accurate, but too slow to
run on every chunk - so it only re-orders the top few dozen candidates of the first stage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from corpus.config import Settings


@runtime_checkable
class Reranker(Protocol):
    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return one relevance score per passage (higher is more relevant)."""
        ...


class CrossEncoderReranker:
    """Local ONNX cross-encoder via ``fastembed`` (no network call at query time)."""

    def __init__(self, model_name: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # optional dependency

        self._model: Any = TextCrossEncoder(model_name=model_name)

    def _score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(passages))]

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return await asyncio.to_thread(self._score, query, passages)


def build_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker == "cross-encoder":
        return CrossEncoderReranker(settings.reranker_model)
    return None
