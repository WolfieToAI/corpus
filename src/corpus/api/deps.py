"""FastAPI dependencies: shared services live on ``app.state`` and are exposed here."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, Security, status
from fastapi.security import APIKeyHeader
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corpus.config import Settings
from corpus.core.embeddings import Embedder
from corpus.core.generation import AnswerGenerator
from corpus.core.reranking import Reranker
from corpus.core.rewriting import QueryRewriter
from corpus.db.models import Collection
from corpus.db.session import system_session, tenant_session
from corpus.observability.metrics import AUTH_FAILURES, RATE_LIMITED
from corpus.services.tenancy import TenantContext, authenticate, consume_rate_limit

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sm


def get_embedder(request: Request) -> Embedder:
    embedder: Embedder = request.app.state.embedder
    return embedder


def get_generator(request: Request) -> AnswerGenerator:
    generator: AnswerGenerator | None = request.app.state.generator
    if generator is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "answer generation is disabled: set LLM_API_KEY and LLM_MODEL",
        )
    return generator


def get_reranker(request: Request) -> Reranker | None:
    reranker: Reranker | None = request.app.state.reranker
    return reranker


def get_rewriter(request: Request) -> QueryRewriter | None:
    rewriter: QueryRewriter | None = request.app.state.rewriter
    return rewriter


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RerankerDep = Annotated[Reranker | None, Depends(get_reranker)]
RewriterDep = Annotated[QueryRewriter | None, Depends(get_rewriter)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]


async def get_system_session(sm: SessionmakerDep) -> AsyncIterator[AsyncSession]:
    async with system_session(sm) as session:
        yield session


SystemSessionDep = Annotated[AsyncSession, Depends(get_system_session)]


WIDGET_ROUTES = frozenset(
    {"/v1/collections/{collection_id}/search", "/v1/collections/{collection_id}/chat"}
)


def _enforce_widget_scope(request: Request, ctx: TenantContext) -> None:
    """A widget key is public (it ships in page source): it may only search and chat, from a
    browser on one of its allowed origins. Its collection is checked in ``get_collection``."""
    route = getattr(request.scope.get("route"), "path", None)
    if route not in WIDGET_ROUTES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "widget keys can only search and chat")
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") not in ctx.allowed_origins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin not allowed for this key")


async def get_tenant(
    request: Request,
    response: Response,
    session: SystemSessionDep,
    provided: Annotated[str | None, Security(_api_key_header)],
) -> TenantContext:
    """Authenticate the API key, check its scope, then charge the tenant's rate limit."""
    ctx = await authenticate(session, provided) if provided else None
    if ctx is None:
        AUTH_FAILURES.inc()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid, revoked or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if ctx.is_widget:
        _enforce_widget_scope(request, ctx)
    decision = await consume_rate_limit(session, ctx)
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_seconds),
    }
    # Tenant id on the span (not on metrics: unbounded cardinality) for per-customer debugging.
    trace.get_current_span().set_attribute("tenant.id", str(ctx.tenant_id))
    if not decision.allowed:
        RATE_LIMITED.inc()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={**headers, "Retry-After": str(decision.reset_after_seconds)},
        )
    response.headers.update(headers)
    return ctx


TenantDep = Annotated[TenantContext, Depends(get_tenant)]


async def get_session(sm: SessionmakerDep, tenant: TenantDep) -> AsyncIterator[AsyncSession]:
    """Request-scoped session bound to the caller's tenant (RLS enforced by PostgreSQL)."""
    async with tenant_session(sm, tenant.tenant_id) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_admin(
    settings: SettingsDep, provided: Annotated[str | None, Security(_admin_key_header)]
) -> None:
    if settings.admin_api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin API is disabled")
    expected = settings.admin_api_key.get_secret_value()
    if provided is None or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin key")


async def get_collection(
    collection_id: uuid.UUID, session: SessionDep, tenant: TenantDep
) -> Collection:
    # Another tenant's collection is invisible under RLS, hence a 404 rather than a 403:
    # the API does not even confirm that the id exists. A widget key sees only its own.
    if tenant.is_widget and collection_id != tenant.collection_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
    collection = await session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found")
    # End the read transaction now: the connection goes back to the pool instead of idling
    # through retrieval and a streamed answer. The next use of the session opens a new
    # transaction, and the after_begin hook re-applies the tenant context.
    await session.commit()
    return collection


EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
GeneratorDep = Annotated[AnswerGenerator, Depends(get_generator)]
CollectionDep = Annotated[Collection, Depends(get_collection)]
