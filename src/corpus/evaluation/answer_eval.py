"""End-to-end answer evaluation with an LLM as a judge.

Each question goes through the production path (rewrite -> retrieve -> generate). A separate
judge call then grades the answer against the retrieved sources and the reference answer, as
validated JSON so that grades are machine-readable:

* **faithful**   - every factual claim is supported by the sources (no hallucination);
* **correct**    - the answer agrees with the reference, or correctly abstains when the
  documents do not contain the answer;
* **citations**  - every ``[n]`` points to a source that supports the sentence it ends.

LLM judges are noisy: treat small differences between runs as noise, and read the judge's
explanations for the failures rather than trusting the aggregate alone.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from xml.sax.saxutils import escape

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import Embedder
from corpus.core.generation import AnswerGenerator, GenerationResult, RefusalError
from corpus.core.prompting import Source, render_sources
from corpus.core.reranking import Reranker
from corpus.core.rewriting import QueryRewriter
from corpus.evaluation.dataset import Dataset, Question
from corpus.evaluation.harness import indexed_corpus
from corpus.services.retrieval import RetrievalParams, retrieve

JUDGE_SYSTEM_PROMPT = """\
You grade answers produced by a document question-answering system. Be strict and literal.

You receive the question, the numbered sources the system retrieved, the system's answer, and a \
reference answer written by a human ("NONE" means the documents do not contain the answer).

- faithful: true only if every factual claim in the answer is supported by the sources. \
Statements that the information is unavailable count as faithful.
- correct: true if the answer conveys the facts of the reference answer. If the reference is \
NONE, true only if the answer says the documents do not contain the information.
- citations_valid: true if every bracketed citation [n] refers to a source that supports the \
sentence it is attached to, and factual sentences are cited. An answer that correctly declines \
without citations is valid.
- explanation: one or two sentences justifying any false grade.\
"""

JUDGE_JSON_INSTRUCTION = """

Reply with a single JSON object and nothing else:
{"faithful": bool, "correct": bool, "citations_valid": bool, "explanation": string}\
"""


class Judgement(BaseModel):
    faithful: bool
    correct: bool
    citations_valid: bool
    explanation: str = Field(max_length=1000)


@dataclass
class AnswerRecord:
    id: str
    type: str
    question: str
    search_query: str
    answer: str
    faithful: bool | None
    correct: bool | None
    citations_valid: bool | None
    explanation: str


@dataclass
class AnswerReport:
    n: int
    faithfulness: float
    correctness: float
    citation_validity: float
    abstention_accuracy: float | None
    by_type: dict[str, float]
    judge_errors: int
    records: list[AnswerRecord]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Judge(Protocol):
    async def judge(
        self, question: Question, sources: Sequence[Source], answer: str
    ) -> Judgement | None: ...


class LLMJudge:
    """Grades with any OpenAI-compatible endpoint. JSON mode is the widely supported way to get
    machine-readable output; the result is still validated against :class:`Judgement`."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def judge(
        self, question: Question, sources: Sequence[Source], answer: str
    ) -> Judgement | None:
        reference = question.answer if question.answer is not None else "NONE"
        content = (
            f"<question>{escape(question.question)}</question>\n\n"
            f"{render_sources(sources)}\n\n"
            f"<answer>{escape(answer)}</answer>\n\n"
            f"<reference>{escape(reference)}</reference>"
        )
        messages: list[Any] = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT + JUDGE_JSON_INSTRUCTION},
            {"role": "user", "content": content},
        ]
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=500,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content if response.choices else None
        if not raw:
            return None
        try:
            return Judgement.model_validate_json(raw)
        except ValidationError:
            return None  # counted as a judge error, never as a grade


def _rate(values: Sequence[bool | None]) -> float:
    graded = [v for v in values if v is not None]
    return statistics.fmean(graded) if graded else 0.0


async def evaluate_answers(
    sm: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
    dataset: Dataset,
    *,
    generator: AnswerGenerator,
    judge: Judge,
    reranker: Reranker | None = None,
    rewriter: QueryRewriter | None = None,
    limit: int | None = None,
) -> AnswerReport:
    questions = list(dataset.questions)[:limit]
    records: list[AnswerRecord] = []
    params = RetrievalParams(
        candidates=settings.retrieval_candidates,
        top_k=settings.retrieval_top_k,
        rrf_k=settings.rrf_k,
        rerank_candidates=settings.rerank_candidates,
    )

    async with indexed_corpus(sm, embedder, settings, dataset) as corpus:
        for q in questions:
            query = q.question
            if q.history and rewriter is not None:
                query = await rewriter.rewrite(q.question, q.history)
            sources = await retrieve(
                sm, embedder, corpus.tenant_id, corpus.collection_id, query, params, reranker
            )
            parts: list[str] = []
            try:
                async for delta in generator.stream_answer(
                    q.question, sources, q.history, GenerationResult()
                ):
                    parts.append(delta)
            except RefusalError:
                parts = ["[the model declined to answer]"]
            answer = "".join(parts)
            verdict = await judge.judge(q, sources, answer)
            records.append(
                AnswerRecord(
                    id=q.id,
                    type=q.type,
                    question=q.question,
                    search_query=query,
                    answer=answer,
                    faithful=verdict.faithful if verdict else None,
                    correct=verdict.correct if verdict else None,
                    citations_valid=verdict.citations_valid if verdict else None,
                    explanation=verdict.explanation if verdict else "judge declined",
                )
            )

    unanswerable = [r.correct for r in records if r.type == "unanswerable"]
    types = sorted({r.type for r in records})
    return AnswerReport(
        n=len(records),
        faithfulness=_rate([r.faithful for r in records]),
        correctness=_rate([r.correct for r in records]),
        citation_validity=_rate([r.citations_valid for r in records]),
        abstention_accuracy=_rate(unanswerable) if unanswerable else None,
        by_type={t: _rate([r.correct for r in records if r.type == t]) for t in types},
        judge_errors=sum(r.faithful is None for r in records),
        records=records,
    )


def render_markdown(report: AnswerReport) -> str:
    lines = [
        "## Answer evaluation (LLM judge)",
        "",
        f"{report.n} questions · judge errors: {report.judge_errors}",
        "",
        "| Metric | Score |",
        "|---|---:|",
        f"| Faithfulness (no unsupported claims) | {report.faithfulness:.1%} |",
        f"| Correctness vs reference | {report.correctness:.1%} |",
        f"| Citation validity | {report.citation_validity:.1%} |",
    ]
    if report.abstention_accuracy is not None:
        lines.append(f"| Abstention on unanswerable questions | {report.abstention_accuracy:.1%} |")
    lines += ["", "| Question type | Correctness |", "|---|---:|"]
    lines += [f"| {t} | {v:.1%} |" for t, v in report.by_type.items()]
    failures = [r for r in report.records if r.correct is False or r.faithful is False]
    if failures:
        lines += ["", "### Failures", ""]
        lines += [f"- **{r.id}** ({r.type}): {r.explanation}" for r in failures]
    return "\n".join(lines) + "\n"
