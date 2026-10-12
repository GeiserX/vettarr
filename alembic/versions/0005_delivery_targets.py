"""Where an approved item was handed, and what the target calls it.

Revision ID: 0005_delivery_targets
Revises: 0004_sender_and_post_time
Create Date: 2026-10-12

Three nullable columns on downloads:

  * delivery_target: radarr, sonarr or seerr. NULL for the download client,
    which is every row written before this; the app reads NULL as
    qbittorrent.
  * remote_id: Radarr's movie id, Sonarr's series id or Seerr's request id.
    NULL for the download client, whose handle stays qbittorrent_hash.
  * remote_changes: what the delivery turned on in Sonarr (the series flag,
    which seasons), so a rejection turns off exactly that. NULL elsewhere.

The target is text, not an enum: a 0.11.x image cannot load an enum label it
has never heard of. It never names these columns, so it still runs on the
migrated database and its inserts leave them NULL.

Additive only, and guarded so a second run is a no-op. No row is read,
rewritten or deleted.
"""

from __future__ import annotations

from alembic import op

revision = "0005_delivery_targets"
down_revision = "0004_sender_and_post_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE downloads
            ADD COLUMN IF NOT EXISTS delivery_target TEXT,
            ADD COLUMN IF NOT EXISTS remote_id TEXT,
            ADD COLUMN IF NOT EXISTS remote_changes JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE downloads
            DROP COLUMN IF EXISTS remote_changes,
            DROP COLUMN IF EXISTS remote_id,
            DROP COLUMN IF EXISTS delivery_target
        """
    )
