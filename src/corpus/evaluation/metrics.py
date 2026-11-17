"""Ranking metrics with evidence-based relevance.

For a question with evidence set ``E`` and a ranked list of retrieved chunks:

* ``recall@k``  fraction of ``E`` found in the top ``k`` chunks (multi-part answers need all).
* ``hit@k``     1 if any relevant chunk is in the top ``k``.
* ``MRR``       1 / rank of the first relevant chunk (0 if none is retrieved).
* ``nDCG@k``    a chunk gains 1 if it brings evidence *not already covered* by a higher-ranked
  chunk, so the same passage retrieved twice (e.g. via overlapping chunks) earns no extra credit;
  the ideal ranking places ``min(|E|, k)`` gains first.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from corpus.evaluation.dataset import normalize

DEFAULT_KS = (1, 3, 5, 10)


@dataclass(frozen=True, slots=True)
class RankingScores:
    recall: Mapping[int, float]
    hit: Mapping[int, float]
    ndcg: Mapping[int, float]
    mrr: float


def _covered(evidence: Sequence[str], text: str) -> set[int]:
    haystack = normalize(text)
    return {i for i, e in enumerate(evidence) if e in haystack}


def score_ranking(
    evidence: Sequence[str], ranked_texts: Sequence[str], ks: Iterable[int] = DEFAULT_KS
) -> RankingScores:
    if not evidence:
        raise ValueError("ranking metrics need at least one evidence string")
    ks = tuple(ks)
    needles = [normalize(e) for e in evidence]

    covered: set[int] = set()
    first_relevant: int | None = None
    gains: list[float] = []
    covered_at: list[int] = []  # |covered| after each rank
    relevant_at: list[bool] = []
    for rank, text in enumerate(ranked_texts, start=1):
        found = _covered(needles, text)
        if found and first_relevant is None:
            first_relevant = rank
        gains.append(1.0 if found - covered else 0.0)
        covered |= found
        covered_at.append(len(covered))
        relevant_at.append(bool(found))

    def at(k: int, series: Sequence[float | int | bool], default: float = 0.0) -> float:
        if not series:
            return default
        return float(series[min(k, len(series)) - 1])

    def dcg(values: Sequence[float]) -> float:
        return sum(v / math.log2(i + 2) for i, v in enumerate(values))

    recall = {k: at(k, covered_at) / len(needles) for k in ks}
    hit = {k: 1.0 if any(relevant_at[:k]) else 0.0 for k in ks}
    ndcg = {k: dcg(gains[:k]) / dcg([1.0] * min(len(needles), k)) for k in ks}
    mrr = 1.0 / first_relevant if first_relevant else 0.0
    return RankingScores(recall=recall, hit=hit, ndcg=ndcg, mrr=mrr)


@dataclass(frozen=True, slots=True)
class AggregateScores:
    n: int
    recall: Mapping[int, float]
    hit: Mapping[int, float]
    ndcg: Mapping[int, float]
    mrr: float


def aggregate(scores: Sequence[RankingScores]) -> AggregateScores:
    if not scores:
        raise ValueError("nothing to aggregate")
    ks = scores[0].recall.keys()
    mean = statistics.fmean
    return AggregateScores(
        n=len(scores),
        recall={k: mean(s.recall[k] for s in scores) for k in ks},
        hit={k: mean(s.hit[k] for s in scores) for k in ks},
        ndcg={k: mean(s.ndcg[k] for s in scores) for k in ks},
        mrr=mean(s.mrr for s in scores),
    )


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, ``q`` in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(q / 100 * len(ordered)) - 1)
    return ordered[index]
