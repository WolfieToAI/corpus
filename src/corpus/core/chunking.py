"""Structure-aware text chunking with token budgets, overlap and page tracking.

The splitter works in two phases:

1. **Recursive split** - each page is cut along the coarsest boundary that keeps every
   piece under the token budget (paragraph -> line -> sentence -> word -> hard cut).
2. **Greedy merge** - consecutive pieces are packed into chunks of at most
   ``max_tokens``; the tail of each chunk is carried over to the next one
   (``overlap_tokens``) so that an idea split across a boundary stays retrievable.

Each chunk remembers the page range it came from, which is what lets the API cite
"report.pdf, p. 4-5" instead of an opaque chunk id.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
"""Heuristic used by :func:`estimate_tokens` (good enough for budgeting, ~±15%)."""

_SEPARATORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n\s*\n"),  # paragraphs
    re.compile(r"\n"),  # lines
    re.compile(r"(?<=[.!?;:])\s+"),  # sentences
    re.compile(r"\s+"),  # words
)
_WHITESPACE_RUN = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class TextChunk:
    ordinal: int
    content: str
    page_start: int
    page_end: int
    token_count: int


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    tokens: int
    page: int


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate. Never returns 0 for non-empty text."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def normalize(text: str) -> str:
    """Collapse horizontal whitespace and strip NUL bytes (common in PDF extraction)."""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return _WHITESPACE_RUN.sub(" ", text).strip()


def _split_recursive(text: str, max_tokens: int, level: int = 0) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if estimate_tokens(text) <= max_tokens:
        return [text]
    if level >= len(_SEPARATORS):
        size = max_tokens * CHARS_PER_TOKEN
        return [text[i : i + size] for i in range(0, len(text), size)]

    parts = [p for p in _SEPARATORS[level].split(text) if p.strip()]
    if len(parts) <= 1:
        return _split_recursive(text, max_tokens, level + 1)

    pieces: list[str] = []
    for part in parts:
        pieces.extend(_split_recursive(part, max_tokens, level + 1))
    return pieces


def chunk_pages(
    pages: Sequence[Page],
    *,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[TextChunk]:
    """Split a document (given page by page) into overlapping, size-bounded chunks."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if not 0 <= overlap_tokens < max_tokens:
        raise ValueError("overlap_tokens must be in [0, max_tokens)")

    pieces = [
        _Piece(text, estimate_tokens(text), page.number)
        for page in pages
        for text in _split_recursive(normalize(page.text), max_tokens)
    ]

    chunks: list[TextChunk] = []
    window: list[_Piece] = []
    window_tokens = 0

    def emit() -> None:
        chunks.append(
            TextChunk(
                ordinal=len(chunks),
                content=" ".join(p.text for p in window),
                page_start=window[0].page,
                page_end=window[-1].page,
                token_count=window_tokens,
            )
        )

    for piece in pieces:
        if window and window_tokens + piece.tokens > max_tokens:
            emit()
            # Carry the tail of the previous chunk over as overlap.
            carried: list[_Piece] = []
            carried_tokens = 0
            for prev in reversed(window):
                if carried_tokens + prev.tokens > overlap_tokens:
                    break
                carried.insert(0, prev)
                carried_tokens += prev.tokens
            window, window_tokens = carried, carried_tokens
            # Never let the overlap push the next chunk over budget.
            while window and window_tokens + piece.tokens > max_tokens:
                window_tokens -= window.pop(0).tokens
        window.append(piece)
        window_tokens += piece.tokens

    if window:
        emit()
    return chunks
