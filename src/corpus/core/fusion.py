"""Rank fusion for hybrid (lexical + semantic) retrieval."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Sequence


def reciprocal_rank_fusion[K: Hashable](
    rankings: Sequence[Sequence[K]],
    *,
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[K, float]]:
    """Merge several ranked lists with Reciprocal Rank Fusion (Cormack et al., 2009).

    ``score(d) = sum_i w_i / (k + rank_i(d))`` with 1-based ranks. RRF only looks at
    ranks, so it needs no score normalisation between BM25-like and cosine scores,
    which live on incomparable scales. Ties are broken by first appearance.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must have one entry per ranking")

    scores: dict[K, float] = defaultdict(float)
    first_seen: dict[K, int] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, item in enumerate(ranking, start=1):
            scores[item] += weight / (k + rank)
            first_seen.setdefault(item, len(first_seen))

    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
