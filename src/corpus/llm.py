"""LLM client construction from settings (OpenAI-compatible protocol, any provider)."""

from __future__ import annotations

from openai import AsyncOpenAI

from corpus.config import Settings


def build_llm_client(settings: Settings) -> AsyncOpenAI | None:
    """A client for the configured endpoint, or ``None`` when no LLM is configured."""
    if settings.llm_api_key is None or not settings.llm_model:
        return None
    return AsyncOpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        max_retries=2,
        timeout=120,
    )
