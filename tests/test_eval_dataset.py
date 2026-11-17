"""Guards on the evaluation dataset itself: a broken label silently corrupts every metric."""

from collections import Counter
from pathlib import Path

import pytest

from corpus.config import Settings
from corpus.core.chunking import Page, chunk_pages
from corpus.evaluation.dataset import load_dataset, normalize

DATASET = load_dataset(Path(__file__).resolve().parents[1] / "eval" / "dataset")
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


def test_dataset_shape() -> None:
    assert len(DATASET.documents) >= 8
    assert len(DATASET.questions) >= 45
    ids = [q.id for q in DATASET.questions]
    assert len(ids) == len(set(ids))
    counts = Counter(q.type for q in DATASET.questions)
    assert counts["unanswerable"] >= 3 and counts["conversational"] >= 4


@pytest.mark.parametrize("question", DATASET.questions, ids=lambda q: q.id)
def test_each_evidence_appears_in_exactly_one_document(question) -> None:  # type: ignore[no-untyped-def]
    for evidence in question.evidence:
        hits = [d.filename for d in DATASET.documents if normalize(evidence) in normalize(d.text)]
        assert len(hits) == 1, f"{question.id}: {evidence!r} found in {hits}"


@pytest.mark.parametrize("question", DATASET.questions, ids=lambda q: q.id)
def test_each_evidence_fits_inside_a_chunk(question) -> None:  # type: ignore[no-untyped-def]
    """An excerpt split across two chunks could never be matched: the label would be unreachable."""
    chunks = [
        normalize(c.content)
        for d in DATASET.documents
        for c in chunk_pages(
            [Page(1, d.text)],
            max_tokens=SETTINGS.chunk_max_tokens,
            overlap_tokens=SETTINGS.chunk_overlap_tokens,
        )
    ]
    for evidence in question.evidence:
        assert any(normalize(evidence) in c for c in chunks), f"{question.id}: {evidence!r}"


def test_labels_are_consistent_with_types() -> None:
    for q in DATASET.questions:
        assert (q.type == "unanswerable") == (not q.evidence), q.id
        assert (q.type == "unanswerable") == (q.answer is None), q.id
        assert (q.type == "conversational") == bool(q.history), q.id
        if q.history:
            assert [t.role for t in q.history][-1] == "assistant", q.id
