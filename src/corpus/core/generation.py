"""Answer generation, streamed token by token: an LLM, or an offline extractive generator."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from openai import AsyncOpenAI

from corpus.core.prompting import SYSTEM_PROMPT, Source, build_user_message


@dataclass(frozen=True, slots=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(slots=True)
class GenerationResult:
    """Filled in by the generator once the stream is exhausted."""

    stop_reason: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class AnswerGenerator(Protocol):
    def stream_answer(
        self,
        question: str,
        sources: Sequence[Source],
        history: Sequence[Turn],
        result: GenerationResult,
    ) -> AsyncIterator[str]: ...


class RefusalError(RuntimeError):
    """The model declined to answer (content filter)."""


def chat_messages(question: str, sources: Sequence[Source], history: Sequence[Turn]) -> list[Any]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *({"role": t.role, "content": t.content} for t in history),
        {"role": "user", "content": build_user_message(question, sources)},
    ]


class ChatCompletionsGenerator:
    """Streams answers from any OpenAI-compatible chat-completions endpoint.

    The wire protocol is the common denominator of hosted providers (OpenAI, Mistral, Groq,
    Together...) and self-hosted servers (vLLM, Ollama, LM Studio): the provider is chosen by
    configuration (``LLM_BASE_URL``, ``LLM_MODEL``), not in code.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> None:
        self._client = client
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def stream_answer(
        self,
        question: str,
        sources: Sequence[Source],
        history: Sequence[Turn],
        result: GenerationResult,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=chat_messages(question, sources, history),
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        finish_reason: str | None = None
        async for chunk in stream:
            if chunk.usage is not None:  # final chunk when include_usage is honoured
                result.input_tokens = chunk.usage.prompt_tokens
                result.output_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta.content:
                yield choice.delta.content
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason
            result.model = chunk.model or self.model

        result.stop_reason = finish_reason
        if finish_reason == "content_filter":
            raise RefusalError("the model declined to answer this question")


# Hard-wrapped lines are joined back unless the next line starts a heading or a list item.
_WRAPPED_LINE = re.compile(r"\n(?!\s*(?:#|[-*]\s|\d+\.\s|\n))")
# Sentence ends, remaining line breaks, and headings / bullets inlined by the chunker.
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+|\s#{1,6}\s|\s[-*]\s")
_MARKUP = re.compile(r"^\s*(#+|[-*]|\d+\.)\s*")


def _term_weight(term: str) -> int:
    """Identifiers (E-317, FLT-4012, 99.9) are far more telling than ordinary words."""
    return 3 if any(c.isdigit() for c in term) else 1


def best_sentences(
    question: str, sources: Sequence[Source], limit: int = 2
) -> list[tuple[str, int]]:
    """Pick the sentences that share the most content terms with the question.

    Returns ``(sentence, source number)`` pairs. Ties favour higher-ranked sources and earlier
    sentences; markdown headings and list markers are stripped.
    """
    from corpus.core.text_search import query_terms  # local: keeps core modules decoupled

    terms = set(query_terms(question))
    scored: list[tuple[int, int, int, str, int]] = []
    for rank, source in enumerate(sources[:3]):
        text = _WRAPPED_LINE.sub(" ", source.content)
        for position, raw in enumerate(_SENTENCE.split(text)):
            sentence = _MARKUP.sub("", raw).strip()
            if len(sentence) < 12:
                continue
            overlap = sum(_term_weight(t) for t in terms & set(query_terms(sentence)))
            scored.append((-overlap, rank, position, sentence, source.number))
    scored.sort()
    if not scored:
        return []
    best = -scored[0][0]
    # Secondary sentences must be nearly as relevant as the best one, or they are noise.
    picked = [s for s in scored if best and -s[0] * 2 >= best][:limit] or scored[:1]
    picked.sort(key=lambda s: (s[1], s[2]))  # read in document order
    return [(sentence, number) for _, _, _, sentence, number in picked]


class ExtractiveGenerator:
    """Answers without an LLM by quoting the sentences of the retrieved passages that best match
    the question, each with its citation.

    Useful for demos without an API key and for load tests, where it isolates the platform's
    own latency (auth, retrieval, streaming) from the LLM's. ``token_delay`` emulates the pace
    of a streaming model when realism matters more than raw throughput.
    """

    model = "extractive"

    def __init__(self, token_delay: float = 0.0, max_sentences: int = 2) -> None:
        self._delay = token_delay
        self._max_sentences = max_sentences

    async def stream_answer(
        self,
        question: str,
        sources: Sequence[Source],
        history: Sequence[Turn],
        result: GenerationResult,
    ) -> AsyncIterator[str]:
        picked = best_sentences(question, sources, self._max_sentences) if sources else []
        if picked:
            text = " ".join(
                f"{s if s.endswith(('.', '!', '?')) else s + '.'} [{n}]" for s, n in picked
            )
        else:
            text = "The documents do not contain this information."
        words = text.split(" ")
        for i, word in enumerate(words):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield word if i == 0 else f" {word}"
        result.model, result.stop_reason = self.model, "end_turn"
        result.output_tokens = len(words)
