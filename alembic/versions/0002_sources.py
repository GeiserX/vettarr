"""Where each item came from: its source, and the post it stands for.

Revision ID: 0002_sources
Revises: 0001_auto_approval
Create Date: 2026-10-11

Two nullable columns on media_items and one partial unique index:

  * source_key: the configured source the post came in through. Rows written
    before sources existed get NULL and keep working: they belong to the
    first userbot source, which is the only chat the app ever read.
  * origin_key: "<chat id>:<message id>" of the post the item stands for, so
    a forwarded channel post that is already an item is not added twice.
    Unique where not null; every existing row is NULL, so the index builds
    without touching a row.

Additive only, and every statement is guarded so a second run is a no-op. A
0.11.x image still runs on the migrated database: it never names these
columns, and its inserts leave them NULL.
"""

from __future__ import annotations

from alembic import op

revision = "0002_sources"
down_revision = "0001_auto_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE media_items
            ADD COLUMN IF NOT EXISTS source_key TEXT,
            ADD COLUMN IF NOT EXISTS origin_key TEXT
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_media_items_origin_key
            ON media_items (origin_key)
            WHERE origin_key IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_media_items_origin_key")
    op.execute(
        """
        ALTER TABLE media_items
            DROP COLUMN IF EXISTS origin_key,
            DROP COLUMN IF EXISTS source_key
        """
    )
