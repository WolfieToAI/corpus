"""Scoped API keys: publishable widget keys bound to one collection and a list of origins.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15

A key embedded in a web page is public by construction. A ``widget`` key can therefore only call
search and chat, on a single collection, from browsers on the origins it lists; everything else
needs a ``full`` (secret, server-side) key.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for sql in (
        "ALTER TABLE api_keys ADD COLUMN scope varchar(16) NOT NULL DEFAULT 'full' "
        "CHECK (scope IN ('full', 'widget'))",
        "ALTER TABLE api_keys ADD COLUMN collection_id uuid "
        "REFERENCES collections (id) ON DELETE CASCADE",
        "ALTER TABLE api_keys ADD COLUMN allowed_origins text[] NOT NULL DEFAULT '{}'",
        # A widget key without a collection or an origin would be either useless or unsafe.
        """
        ALTER TABLE api_keys ADD CONSTRAINT ck_widget_key_is_bound CHECK (
            scope = 'full'
            OR (collection_id IS NOT NULL AND cardinality(allowed_origins) > 0)
        )
        """,
    ):
        op.execute(sql)


def downgrade() -> None:
    for sql in (
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS ck_widget_key_is_bound",
        "ALTER TABLE api_keys DROP COLUMN IF EXISTS allowed_origins",
        "ALTER TABLE api_keys DROP COLUMN IF EXISTS collection_id",
        "ALTER TABLE api_keys DROP COLUMN IF EXISTS scope",
    ):
        op.execute(sql)
