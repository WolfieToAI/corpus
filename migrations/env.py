"""Alembic environment: async engine, URL from settings unless the caller overrides it."""

from __future__ import annotations

import asyncio
import logging.config

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from corpus.config import get_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    logging.config.fileConfig(config.config_file_name)

url = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
