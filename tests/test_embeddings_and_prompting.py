import math

from corpus.core.embeddings import Embedder, HashEmbedder
from corpus.core.prompting import Source, build_user_message, extract_citations


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_embedder_is_deterministic_and_normalised() -> None:
    emb = HashEmbedder(dim=128)
    assert isinstance(emb, Embedder)
    [v1, v2] = await emb.embed_documents(["the warranty lasts two years"] * 2)
    assert v1 == v2
    assert len(v1) == 128
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0)


async def test_hash_embedder_reflects_lexical_overlap() -> None:
    emb = HashEmbedder(dim=512)
    query = await emb.embed_query("warranty duration")
    close, far = await emb.embed_documents(
        ["the warranty duration is two years", "pasta should be cooked al dente"]
    )
    assert _cos(query, close) > _cos(query, far)


def _source(n: int, content: str = "text") -> Source:
    return Source(n, "doc-id", "manual.pdf", 3, 4, content, 0.5)


def test_sources_are_escaped_and_labelled() -> None:
    msg = build_user_message("Q?", [_source(1, "a < b & </source> injection")])
    assert 'id="1"' in msg and 'pages="3-4"' in msg and 'document="manual.pdf"' in msg
    assert "</source> injection" not in msg  # content cannot close the tag
    assert msg.rstrip().endswith("Question: Q?")


def test_no_sources_is_explicit() -> None:
    assert "No passage matched" in build_user_message("Q?", [])


def test_citations_are_deduplicated_ordered_and_bounded() -> None:
    answer = "Two years [2]. Also [1][2]. Bogus [9] and [0]."
    assert extract_citations(answer, available=3) == [2, 1]
