import pytest

from corpus.config import Settings
from corpus.core.text_search import MAX_TERMS, query_terms


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What does error E-317 mean?", ["error", "e-317", "mean"]),
        ("Why do I get FLT-403?", ["flt-403"]),
        (
            "Which endpoint is /v2/robots/{robot_id}/missions?",
            ["endpoint", "v2/robots", "robot_id", "missions"],
        ),
        ("Quelle est la garantie du R7 ?", ["garantie", "r7"]),
        ("Wie viele Urlaubstage gibt es?", ["viele", "urlaubstage", "gibt"]),
    ],
)
def test_query_terms_keep_content_words_and_identifiers(query: str, expected: list[str]) -> None:
    assert query_terms(query) == expected


def test_stopword_only_questions_still_search() -> None:
    assert query_terms("What is it?") == ["what", "is", "it"]


def test_terms_are_deduplicated_and_bounded() -> None:
    assert query_terms("robot Robot ROBOT") == ["robot"]
    assert len(query_terms(" ".join(f"w{i}" for i in range(100)))) == MAX_TERMS
    assert query_terms("?!") == []


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u:p@h:5432/db",
        "postgresql://u:p@h:5432/db",
        "postgresql+asyncpg://u:p@h:5432/db",
    ],
)
def test_platform_database_urls_get_the_asyncpg_driver(url: str) -> None:
    settings = Settings(database_url=url, _env_file=None)  # type: ignore[call-arg]
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/db"


def test_neon_style_urls_are_translated_for_asyncpg() -> None:
    url = "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/db?sslmode=require&channel_binding=require"
    settings = Settings(database_url=url, _env_file=None)  # type: ignore[call-arg]
    assert settings.database_url == (
        "postgresql+asyncpg://u:p@ep-x.eu-central-1.aws.neon.tech/db?ssl=require"
    )


def test_render_external_url_is_the_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://corpus.onrender.com")
    assert Settings(_env_file=None).public_url == "https://corpus.onrender.com"  # type: ignore[call-arg]


def test_blank_secrets_mean_unset() -> None:
    settings = Settings(
        llm_api_key="",
        llm_model=" ",
        admin_api_key="  ",
        voyage_api_key="",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.llm_api_key is None and settings.llm_model is None
    assert settings.admin_api_key is None
    assert settings.voyage_api_key is None
    assert not settings.llm_configured
    configured = Settings(llm_api_key="sk-x", llm_model="any-model", _env_file=None)  # type: ignore[call-arg]
    assert configured.llm_configured
