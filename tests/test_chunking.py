import itertools

import pytest

from corpus.core.chunking import Page, chunk_pages, estimate_tokens, normalize


def _paragraphs(n: int, words: int = 40) -> str:
    return "\n\n".join(" ".join(f"word{p}_{w}" for w in range(words)) + "." for p in range(n))


def test_empty_document_gives_no_chunks() -> None:
    assert chunk_pages([Page(1, "   \n\n ")]) == []


def test_short_text_is_a_single_chunk() -> None:
    chunks = chunk_pages([Page(1, "Hello world. This is short.")])
    assert len(chunks) == 1
    assert chunks[0].content == "Hello world. This is short."
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)


@pytest.mark.parametrize("max_tokens", [50, 120, 400])
def test_every_chunk_respects_the_budget(max_tokens: int) -> None:
    chunks = chunk_pages([Page(1, _paragraphs(30))], max_tokens=max_tokens, overlap_tokens=10)
    assert len(chunks) > 1
    assert all(c.token_count <= max_tokens for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_consecutive_chunks_overlap() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_pages([Page(1, text)], max_tokens=60, overlap_tokens=20)
    for prev, nxt in itertools.pairwise(chunks):
        tail = prev.content.split(". ")[-1]
        assert tail in nxt.content


def test_no_overlap_means_no_duplicated_text() -> None:
    text = " ".join(f"s{i}." for i in range(500))
    chunks = chunk_pages([Page(1, text)], max_tokens=40, overlap_tokens=0)
    rebuilt = " ".join(c.content for c in chunks)
    assert rebuilt == normalize(text)


def test_page_ranges_are_tracked() -> None:
    pages = [Page(i, f"Content of page {i}. " * 30) for i in range(1, 6)]
    chunks = chunk_pages(pages, max_tokens=300, overlap_tokens=0)
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 5
    assert any(c.page_start != c.page_end for c in chunks)  # some chunk spans two pages
    assert all(c.page_start <= c.page_end for c in chunks)


def test_unbreakable_token_is_hard_split() -> None:
    blob = "x" * 5000
    chunks = chunk_pages([Page(1, blob)], max_tokens=100, overlap_tokens=0)
    assert "".join(c.content.replace(" ", "") for c in chunks) == blob
    assert all(c.token_count <= 100 for c in chunks)


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_pages([Page(1, "x")], max_tokens=10, overlap_tokens=10)
    with pytest.raises(ValueError):
        chunk_pages([Page(1, "x")], max_tokens=0)


def test_estimate_and_normalize() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert normalize("a\x00b \t\t c\r\nd") == "ab c\nd"
