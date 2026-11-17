"""Conversational query rewriting.

"And for the R7?" is a fine follow-up for a human but a useless search query: it contains none
of the words of the passage that answers it. Before retrieval, follow-up questions are rewritten
into standalone queries using the conversation history. Generation still receives the user's
original question and the full history.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol
from xml.sax.saxutils import escape

from openai import AsyncOpenAI

from corpus.core.generation import Turn

logger = logging.getLogger(__name__)

REWRITE_SYSTEM_PROMPT = """\
You turn the last question of a conversation into a standalone search query for a document \
search engine.

- Resolve pronouns and ellipses using the conversation ("there", "it", "and for the R7?").
- Keep product names, error codes, identifiers and numbers exactly as written.
- Keep the language of the question.
- If the question is already standalone, return it unchanged.
- Output only the query, on one line, without quotes or explanations.\
"""

MAX_QUERY_CHARS = 500


class QueryRewriter(Protocol):
    async def rewrite(self, question: str, history: Sequence[Turn]) -> str: ...


def render_conversation(question: str, history: Sequence[Turn]) -> str:
    turns = "\n".join(f"<{t.role}>{escape(t.content)}</{t.role}>" for t in history)
    return f"<conversation>\n{turns}\n</conversation>\n\nLast question: {escape(question)}"


def clean_query(raw: str, fallback: str) -> str:
    query = " ".join(raw.split()).strip().strip("\"'`")
    return query[:MAX_QUERY_CHARS] if query else fallback


class LLMQueryRewriter:
    """Rewrites follow-ups with any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, client: AsyncOpenAI, *, model: str) -> None:
        self._client = client
        self._model = model

    async def rewrite(self, question: str, history: Sequence[Turn]) -> str:
        if not history:
            return question
        messages: list[Any] = [
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": render_conversation(question, history)},
        ]
        response = await self._client.chat.completions.create(
            model=self._model, messages=messages, max_tokens=200, temperature=0
        )
        choice = response.choices[0] if response.choices else None
        if choice is None or choice.finish_reason == "content_filter":
            # Retrieval with the raw question is a safe degradation, not an error.
            logger.info("query rewriting unavailable; using the original question")
            return question
        return clean_query(choice.message.content or "", fallback=question)
