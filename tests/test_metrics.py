import pytest

from corpus.evaluation.metrics import aggregate, percentile, score_ranking


def test_perfect_ranking_scores_one() -> None:
    s = score_ranking(["warranty is 2 years"], ["The warranty is 2 years.", "noise"], ks=(1, 3))
    assert s.mrr == 1.0
    assert s.recall == {1: 1.0, 3: 1.0}
    assert s.hit == {1: 1.0, 3: 1.0}
    assert s.ndcg == {1: 1.0, 3: 1.0}


def test_relevant_at_rank_two() -> None:
    s = score_ranking(["needle"], ["hay", "a NEEDLE here", "hay"], ks=(1, 3))
    assert s.mrr == 0.5
    assert s.hit == {1: 0.0, 3: 1.0}
    assert s.ndcg[3] == pytest.approx(1 / 1.5849625007)  # 1 / log2(3)


def test_multi_evidence_recall_is_partial_until_all_found() -> None:
    s = score_ranking(["alpha", "beta"], ["alpha", "noise", "beta"], ks=(1, 2, 3))
    assert s.recall == {1: 0.5, 2: 0.5, 3: 1.0}
    assert s.mrr == 1.0


def test_duplicate_evidence_earns_no_extra_gain() -> None:
    # Two chunks containing the same passage: the second one must not count again.
    once = score_ranking(["alpha", "beta"], ["alpha", "alpha again", "beta"], ks=(3,))
    ideal = score_ranking(["alpha", "beta"], ["alpha", "beta", "noise"], ks=(3,))
    assert ideal.ndcg[3] == pytest.approx(1.0)
    assert once.ndcg[3] < ideal.ndcg[3]


def test_matching_ignores_case_and_whitespace() -> None:
    s = score_ranking(
        ["Fixed   false E-204\nerrors"], ["- fixed false e-204 errors on dark floors"]
    )
    assert s.mrr == 1.0


def test_nothing_retrieved() -> None:
    s = score_ranking(["x"], [], ks=(1, 5))
    assert s.mrr == 0.0 and s.recall == {1: 0.0, 5: 0.0} and s.ndcg == {1: 0.0, 5: 0.0}


def test_no_evidence_is_rejected() -> None:
    with pytest.raises(ValueError):
        score_ranking([], ["anything"])


def test_aggregate_and_percentile() -> None:
    a = score_ranking(["x"], ["x"], ks=(1,))
    b = score_ranking(["x"], ["y", "x"], ks=(1,))
    agg = aggregate([a, b])
    assert agg.n == 2 and agg.mrr == pytest.approx(0.75) and agg.hit[1] == 0.5
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 95) == 5.0
    assert percentile([], 50) == 0.0
