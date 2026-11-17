"""Operator routes: tenant and API-key lifecycle, queue statistics (``X-Admin-Key``)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from corpus.api.deps import SettingsDep, SystemSessionDep, require_admin
from corpus.api.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    JobStats,
    TenantCreate,
    TenantOut,
)
from corpus.db.models import ApiKey, Collection, Tenant
from corpus.jobs import queue
from corpus.services import tenancy

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate, session: SystemSessionDep, settings: SettingsDep
) -> Tenant:
    try:
        tenant = await tenancy.create_tenant(
            session, body.name, body.rate_limit_per_minute or settings.default_rate_limit_per_minute
        )
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "a tenant with this name exists") from exc
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(session: SystemSessionDep) -> list[Tenant]:
    return list((await session.scalars(select(Tenant).order_by(Tenant.created_at))).all())


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(tenant_id: uuid.UUID, session: SystemSessionDep) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    await session.delete(tenant)  # cascades to every row the tenant owns
    await session.commit()


@router.post(
    "/tenants/{tenant_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    tenant_id: uuid.UUID, body: ApiKeyCreate, session: SystemSessionDep
) -> ApiKeyCreated:
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    if body.collection_id is not None:
        collection = await session.get(Collection, body.collection_id)
        if collection is None or collection.tenant_id != tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "collection not found for this tenant")
    key, plaintext = await tenancy.create_api_key(
        session,
        tenant_id,
        body.name,
        scope=body.scope,
        collection_id=body.collection_id,
        allowed_origins=body.allowed_origins,
    )
    await session.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=plaintext)


@router.get("/tenants/{tenant_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(tenant_id: uuid.UUID, session: SystemSessionDep) -> list[ApiKey]:
    stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
    return list((await session.scalars(stmt)).all())


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: uuid.UUID, session: SystemSessionDep) -> None:
    if not await tenancy.revoke_api_key(session, key_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active key with this id")
    await session.commit()


@router.get("/jobs/stats", response_model=JobStats)
async def job_stats(session: SystemSessionDep) -> JobStats:
    return JobStats(**await queue.stats(session))
