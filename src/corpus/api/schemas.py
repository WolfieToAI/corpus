"""Request / response models of the public HTTP API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from corpus.db.models import DocumentStatus


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["product-docs"])
    description: str | None = Field(default=None, max_length=2000)


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    collection_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    error: str | None
    page_count: int | None
    chunk_count: int | None
    created_at: datetime
    updated_at: datetime


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    mode: Literal["hybrid", "vector", "keyword"] = Field(
        default="hybrid", description="Candidate generation; non-hybrid modes are for debugging."
    )
    rerank: bool = Field(default=True, description="Apply the cross-encoder, if one is enabled.")


class SourceOut(BaseModel):
    number: int
    document_id: str
    filename: str
    page_start: int
    page_end: int
    content: str
    score: float


class SearchResponse(BaseModel):
    query: str
    sources: list[SourceOut]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("history")
    @classmethod
    def _alternating(cls, history: list[ChatTurn]) -> list[ChatTurn]:
        # The Messages API expects user/assistant alternation, and the new question is a
        # user turn, so the history must start with "user" and end with "assistant".
        for i, turn in enumerate(history):
            expected = "user" if i % 2 == 0 else "assistant"
            if turn.role != expected:
                raise ValueError("history must alternate user/assistant, starting with user")
        if history and history[-1].role != "assistant":
            raise ValueError("history must end with an assistant turn")
        return history


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["acme"])
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    rate_limit_per_minute: int
    created_at: datetime


_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=120)
    scope: Literal["full", "widget"] = Field(
        default="full",
        description="'widget': publishable key for the browser widget (search and chat only).",
    )
    collection_id: uuid.UUID | None = None
    allowed_origins: list[str] = Field(
        default_factory=list, max_length=20, examples=[["https://www.example.com"]]
    )

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, origins: list[str]) -> list[str]:
        cleaned = [o.rstrip("/") for o in origins]
        for origin in cleaned:
            if not _ORIGIN.match(origin):
                raise ValueError(f"{origin!r} is not an origin (scheme://host[:port])")
        return cleaned

    @model_validator(mode="after")
    def _widget_keys_are_bound(self) -> ApiKeyCreate:
        if self.scope == "widget" and (self.collection_id is None or not self.allowed_origins):
            raise ValueError("widget keys need a collection_id and at least one allowed origin")
        if self.scope == "full" and (self.collection_id or self.allowed_origins):
            raise ValueError("collection_id and allowed_origins only apply to widget keys")
        return self


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    prefix: str
    scope: str
    collection_id: uuid.UUID | None
    allowed_origins: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full key. It is shown only once and never stored.")


class JobStats(BaseModel):
    queued: int
    running: int
    succeeded: int
    dead: int


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    version: str
