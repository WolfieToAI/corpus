"""Pluggable embedding backends behind a single async protocol.

* ``HashEmbedder`` - deterministic feature hashing. No model, no network: used in tests
  and for zero-dependency demos. Captures lexical overlap only.
* ``FastEmbedEmbedder`` - local ONNX models via ``fastembed`` (multilingual by default).
* ``VoyageEmbedder`` - Voyage AI hosted embeddings.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from corpus.config import Settings

Vector = list[float]
_TOKEN = re.compile(r"\w+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]: ...

    async def embed_query(self, text: str) -> Vector: ...


def _l2_normalize(vec: Vector) -> Vector:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class HashEmbedder:
    """Signed feature hashing of unigrams + bigrams into a fixed-size, L2-normalised vector."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _embed(self, text: str) -> Vector:
        tokens = [t.lower() for t in _TOKEN.findall(text)]
        features = tokens + [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
        vec = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "little")
            vec[value % self.dim] += 1.0 if (value >> 63) & 1 else -1.0
        return _l2_normalize(vec)

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._embed(text)


class FastEmbedEmbedder:
    """Local CPU embeddings. The model is downloaded once, then cached on disk."""

    def __init__(self, model_name: str, dim: int) -> None:
        from fastembed import TextEmbedding  # optional dependency, imported lazily

        self.dim = dim
        self._model: Any = TextEmbedding(model_name=model_name)

    def _run(self, texts: Sequence[str]) -> list[Vector]:
        vectors = [[float(x) for x in v] for v in self._model.embed(list(texts))]
        if vectors and len(vectors[0]) != self.dim:
            raise ValueError(
                f"model returned {len(vectors[0])}-d vectors but EMBEDDING_DIM={self.dim}"
            )
        return vectors

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        # ONNX inference is CPU-bound: keep it off the event loop.
        return await asyncio.to_thread(self._run, texts)

    async def embed_query(self, text: str) -> Vector:
        return (await self.embed_documents([text]))[0]


class VoyageEmbedder:
    _URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str, dim: int, timeout: float = 30.0) -> None:
        self.dim = dim
        self._model = model
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
        )

    async def _embed(self, texts: Sequence[str], input_type: str) -> list[Vector]:
        response = await self._client.post(
            self._URL,
            json={
                "input": list(texts),
                "model": self._model,
                "input_type": input_type,
                "output_dimension": self.dim,
            },
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> Vector:
        return (await self._embed([text], "query"))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


def build_embedder(settings: Settings) -> Embedder:
    match settings.embedding_provider:
        case "hash":
            return HashEmbedder(settings.embedding_dim)
        case "fastembed":
            return FastEmbedEmbedder(settings.embedding_model, settings.embedding_dim)
        case "voyage":
            assert settings.voyage_api_key is not None  # enforced by Settings validation
            return VoyageEmbedder(
                settings.voyage_api_key.get_secret_value(),
                settings.embedding_model,
                settings.embedding_dim,
            )
