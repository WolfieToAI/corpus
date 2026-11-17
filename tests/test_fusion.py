import pytest

from corpus.core.fusion import reciprocal_rank_fusion


def test_item_ranked_well_by_both_wins() -> None:
    semantic = ["a", "b", "c"]
    lexical = ["b", "d", "a"]
    fused = [item for item, _ in reciprocal_rank_fusion([semantic, lexical], k=60)]
    assert fused[0] == "b"  # 1/62 + 1/61 beats a's 1/61 + 1/63
    assert set(fused) == {"a", "b", "c", "d"}


def test_scores_match_formula() -> None:
    fused = dict(reciprocal_rank_fusion([["x"], ["y", "x"]], k=10))
    assert fused["x"] == pytest.approx(1 / 11 + 1 / 12)
    assert fused["y"] == pytest.approx(1 / 11)


def test_ties_keep_first_appearance_order() -> None:
    fused = [item for item, _ in reciprocal_rank_fusion([["p"], ["q"]])]
    assert fused == ["p", "q"]


def test_weights_shift_the_balance() -> None:
    fused = reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0, 2.0])
    assert fused[0][0] == "b"


def test_empty_and_invalid_inputs() -> None:
    assert reciprocal_rank_fusion([[], []]) == []
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=0)
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], weights=[1.0, 2.0])
