from corpus.core.generation import Turn
from corpus.core.rewriting import MAX_QUERY_CHARS, clean_query, render_conversation


def test_conversation_is_rendered_and_escaped() -> None:
    rendered = render_conversation(
        "And for <the> R7?",
        [Turn("user", "Warranty of the R5?"), Turn("assistant", "2 years [1] & battery")],
    )
    assert "<user>Warranty of the R5?</user>" in rendered
    assert "<assistant>2 years [1] &amp; battery</assistant>" in rendered
    assert rendered.endswith("Last question: And for &lt;the&gt; R7?")


def test_clean_query_strips_quotes_and_whitespace() -> None:
    assert clean_query('  "What is the R7 warranty?"\n', "fallback") == "What is the R7 warranty?"


def test_clean_query_falls_back_on_empty_output() -> None:
    assert clean_query("  \n ", "original question") == "original question"


def test_clean_query_bounds_length() -> None:
    assert len(clean_query("x" * 5000, "f")) == MAX_QUERY_CHARS
