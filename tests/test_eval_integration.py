"""The evaluation harness runs end to end on the real dataset (with offline fakes)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import HashEmbedder
from corpus.core.generation import GenerationResult, Turn
from corpus.core.prompting import Source
from corpus.db.models import Tenant
from corpus.db.session import system_session
from corpus.evaluation import answer_eval, retrieval_eval
from corpus.evaluation.answer_eval import Judgement
from corpus.evaluation.dataset import Question, load_dataset

pytestmark = pytest.mark.integration

DATASET = load_dataset(Path(__file__).resolve().parents[1] / "eval" / "dataset")


class OverlapReranker:
    """Cheap lexical reranker standing in for the cross-encoder."""

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        words = set(query.lower().split())
        return [len(words & set(p.lower().split())) for p in passages]  # type: ignore[misc]


class EchoRewriter:
    async def rewrite(self, question: str, history: Sequence[Turn]) -> str:
        return f"{history[0].content} {question}" if history else question


class QuoteGenerator:
    async def stream_answer(
        self,
        question: str,
        sources: Sequence[Source],
        history: Sequence[Turn],
        result: GenerationResult,
    ) -> AsyncIterator[str]:
        yield (sources[0].content[:80] + " [1]") if sources else "Not in the documents."


class LenientJudge:
    async def judge(self, question: Question, sources: Sequence[Source], answer: str) -> Judgement:
        return Judgement(
            faithful=True,
            correct=question.answerable,
            citations_valid=True,
            explanation="" if question.answerable else "should have abstained",
        )


async def _tenants(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    async with system_session(sm) as s:
        return list((await s.scalars(select(Tenant.name))).all())


async def test_retrieval_evaluation_report(
    sm: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    report = await retrieval_eval.evaluate_retrieval(
        sm,
        HashEmbedder(settings.embedding_dim),
        settings,
        DATASET,
        reranker=OverlapReranker(),
        rewriter=EchoRewriter(),
    )
    assert report.questions == len(DATASET.of_type("paraphrase", "keyword", "multi"))
    assert report.documents == len(DATASET.documents) and report.chunks >= report.documents
    assert [r.strategy for r in report.results] == [s.name for s in retrieval_eval.STRATEGIES]
    for r in report.results:
        assert 0.0 <= r.overall.mrr <= 1.0
        assert r.overall.recall[1] <= r.overall.recall[5] <= r.overall.recall[10]
    # Even lexical hashing finds exact error codes: a sanity floor for the whole pipeline.
    keyword = next(r for r in report.results if r.strategy == "Full-text only")
    assert keyword.by_type["keyword"].hit[5] >= 0.8

    conv = next(iter(report.conversational.values()))
    assert conv.rewritten is not None and conv.raw.n == len(DATASET.of_type("conversational"))

    markdown = retrieval_eval.render_markdown(report)
    assert "| Hybrid + cross-encoder |" in markdown and "Recall@5" in markdown
    assert await _tenants(sm) == []  # the temporary tenant is cleaned up


async def test_answer_evaluation_report(
    sm: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    report = await answer_eval.evaluate_answers(
        sm,
        HashEmbedder(settings.embedding_dim),
        settings,
        DATASET,
        generator=QuoteGenerator(),
        judge=LenientJudge(),
        rewriter=EchoRewriter(),
    )
    assert report.n == len(DATASET.questions)
    assert report.faithfulness == 1.0
    assert report.abstention_accuracy == 0.0  # the lenient judge fails every abstention
    assert report.by_type["unanswerable"] == 0.0
    assert "should have abstained" in answer_eval.render_markdown(report)
    assert await _tenants(sm) == []
