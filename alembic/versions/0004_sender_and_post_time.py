"""Who posted each item, and when.

Revision ID: 0004_sender_and_post_time
Revises: 0003_policy_engine
Create Date: 2026-10-12

Two nullable columns on media_items:

  * sender_id: the Telegram user id of whoever sent the message (for a
    forward, who forwarded it). NULL when no person did (a channel post, an
    anonymous admin). The `sender` and `per_sender_per_week` signals read it,
    and so does `direct_adds`, which decides whose own torrent is added as it
    is.
  * posted_at: when the message was sent, as Telegram dates it. created_at is
    when the app read it, which for a backfilled post can be years later.
    The `post_age_hours` signal reads it.

Existing rows keep NULL in both and keep working: their sender and post time
read as missing, which is what they were before this.

Additive only, and guarded so a second run is a no-op. No row is read,
rewritten or deleted. A 0.11.x image still runs on the migrated database: it
never names these columns, and its inserts leave them NULL.
"""

from __future__ import annotations

from alembic import op

revision = "0004_sender_and_post_time"
down_revision = "0003_policy_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE media_items
            ADD COLUMN IF NOT EXISTS sender_id BIGINT,
            ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE media_items
            DROP COLUMN IF EXISTS posted_at,
            DROP COLUMN IF EXISTS sender_id
        """
    )
