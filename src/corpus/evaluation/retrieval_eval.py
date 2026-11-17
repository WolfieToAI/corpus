"""Retrieval evaluation: compare candidate-generation and re-ranking strategies."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import Embedder
from corpus.core.reranking import Reranker
from corpus.core.rewriting import QueryRewriter
from corpus.evaluation.dataset import Dataset, Question
from corpus.evaluation.harness import IndexedCorpus, indexed_corpus
from corpus.evaluation.metrics import (
    DEFAULT_KS,
    AggregateScores,
    RankingScores,
    aggregate,
    percentile,
    score_ranking,
)
from corpus.services.retrieval import RetrievalMode, RetrievalParams, retrieve

DEPTH = max(DEFAULT_KS)


@dataclass(frozen=True, slots=True)
class Strategy:
    name: str
    mode: RetrievalMode
    rerank: bool = False


STRATEGIES = (
    Strategy("Full-text only", "keyword"),
    Strategy("Vector only", "vector"),
    Strategy("Hybrid (RRF)", "hybrid"),
    Strategy("Hybrid + cross-encoder", "hybrid", rerank=True),
)


@dataclass
class StrategyResult:
    strategy: str
    overall: AggregateScores
    by_type: dict[str, AggregateScores]
    latency_ms_p50: float
    latency_ms_p95: float
    misses: list[str] = field(default_factory=list)  # question ids with nothing relevant in top 5


@dataclass
class ConversationalResult:
    raw: AggregateScores
    rewritten: AggregateScores | None


@dataclass
class RetrievalReport:
    questions: int
    documents: int
    chunks: int
    embedding_model: str
    reranker_model: str | None
    results: list[StrategyResult]
    conversational: dict[str, ConversationalResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _run(
    sm: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    corpus: IndexedCorpus,
    question: str,
    strategy: Strategy,
    reranker: Reranker | None,
    settings: Settings,
) -> tuple[list[str], float]:
    params = RetrievalParams(
        mode=strategy.mode,
        candidates=settings.retrieval_candidates,
        top_k=DEPTH,
        rrf_k=settings.rrf_k,
        rerank_candidates=max(DEPTH, settings.rerank_candidates),
    )
    started = time.perf_counter()
    sources = await retrieve(
        sm,
        embedder,
        corpus.tenant_id,
        corpus.collection_id,
        question,
        params,
        reranker=reranker if strategy.rerank else None,
    )
    return [s.content for s in sources], (time.perf_counter() - started) * 1000


async def evaluate_retrieval(
    sm: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
    dataset: Dataset,
    *,
    reranker: Reranker | None = None,
    rewriter: QueryRewriter | None = None,
) -> RetrievalReport:
    standalone = dataset.of_type("paraphrase", "keyword", "multi")
    conversational = dataset.of_type("conversational")
    strategies = [s for s in STRATEGIES if reranker is not None or not s.rerank]

    async with indexed_corpus(sm, embedder, settings, dataset) as corpus:
        results: list[StrategyResult] = []
        for strategy in strategies:
            scores: list[tuple[Question, RankingScores]] = []
            latencies: list[float] = []
            for q in standalone:
                texts, ms = await _run(
                    sm, embedder, corpus, q.question, strategy, reranker, settings
                )
                scores.append((q, score_ranking(q.evidence, texts)))
                latencies.append(ms)
            by_type = {
                t: aggregate([s for q, s in scores if q.type == t])
                for t in ("paraphrase", "keyword", "multi")
            }
            results.append(
                StrategyResult(
                    strategy=strategy.name,
                    overall=aggregate([s for _, s in scores]),
                    by_type=by_type,
                    latency_ms_p50=round(percentile(latencies, 50), 1),
                    latency_ms_p95=round(percentile(latencies, 95), 1),
                    misses=[q.id for q, s in scores if s.hit[5] == 0.0],
                )
            )

        # Follow-up questions: the raw text vs the query rewritten from the conversation.
        best = strategies[-1]
        conv: dict[str, ConversationalResult] = {}
        raw_scores: list[RankingScores] = []
        rewritten_scores: list[RankingScores] = []
        for q in conversational:
            texts, _ = await _run(sm, embedder, corpus, q.question, best, reranker, settings)
            raw_scores.append(score_ranking(q.evidence, texts))
            if rewriter is not None:
                query = await rewriter.rewrite(q.question, q.history)
                texts, _ = await _run(sm, embedder, corpus, query, best, reranker, settings)
                rewritten_scores.append(score_ranking(q.evidence, texts))
        if raw_scores:
            conv[best.name] = ConversationalResult(
                raw=aggregate(raw_scores),
                rewritten=aggregate(rewritten_scores) if rewritten_scores else None,
            )

        return RetrievalReport(
            questions=len(standalone),
            documents=len(dataset.documents),
            chunks=corpus.chunk_count,
            embedding_model=f"{settings.embedding_provider}:{settings.embedding_model}",
            reranker_model=settings.reranker_model if reranker is not None else None,
            results=results,
            conversational=conv,
        )


def _fmt(x: float) -> str:
    return f"{x:.3f}"


def render_markdown(report: RetrievalReport, ks: Sequence[int] = (1, 3, 5)) -> str:
    lines = [
        "## Retrieval evaluation",
        "",
        f"{report.questions} questions · {report.documents} documents · {report.chunks} chunks · "
        f"embeddings `{report.embedding_model}`"
        + (f" · reranker `{report.reranker_model}`" if report.reranker_model else ""),
        "",
        "| Strategy | "
        + " | ".join(f"Recall@{k}" for k in ks)
        + " | MRR | nDCG@5 | p50 ms | p95 ms |",
        "|---|" + "---:|" * (len(ks) + 4),
    ]
    for r in report.results:
        lines.append(
            f"| {r.strategy} | "
            + " | ".join(_fmt(r.overall.recall[k]) for k in ks)
            + f" | {_fmt(r.overall.mrr)} | {_fmt(r.overall.ndcg[5])}"
            + f" | {r.latency_ms_p50:.0f} | {r.latency_ms_p95:.0f} |"
        )

    lines += ["", "### MRR by question type", ""]
    types = list(report.results[0].by_type)
    lines += [
        "| Strategy | "
        + " | ".join(f"{t} (n={report.results[0].by_type[t].n})" for t in types)
        + " |",
        "|---|" + "---:|" * len(types),
    ]
    for r in report.results:
        lines.append(
            f"| {r.strategy} | " + " | ".join(_fmt(r.by_type[t].mrr) for t in types) + " |"
        )

    for name, c in report.conversational.items():
        lines += ["", f"### Follow-up questions ({name}, n={c.raw.n})", ""]
        lines += ["| Query sent to retrieval | Recall@5 | MRR |", "|---|---:|---:|"]
        lines.append(f"| Raw follow-up | {_fmt(c.raw.recall[5])} | {_fmt(c.raw.mrr)} |")
        if c.rewritten is not None:
            rw = c.rewritten
            lines.append(f"| Rewritten with history | {_fmt(rw.recall[5])} | {_fmt(rw.mrr)} |")
        else:
            lines.append("| Rewritten with history | n/a (needs an LLM: LLM_API_KEY) | |")

    misses = {r.strategy: r.misses for r in report.results if r.misses}
    if misses:
        lines += ["", "### Questions with no relevant chunk in the top 5", ""]
        lines += [f"- {name}: {', '.join(ids)}" for name, ids in misses.items()]
    return "\n".join(lines) + "\n"
