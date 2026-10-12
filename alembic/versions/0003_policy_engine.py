"""Policy engine: structured reasons, the signal snapshot, and the events table.

Revision ID: 0003_policy_engine
Revises: 0002_sources
Create Date: 2026-10-11

Additive only, so a 0.11.x image still runs on a database migrated by this:

  * four nullable columns on media_items (reasons, policy_version, signals,
    policy_outcome). Existing rows keep NULL, which the app reads as a
    decision made before the policy engine ("legacy"); nothing re-runs them.
  * a new events table. No existing table changes shape.

Every statement is guarded, so a second run is a no-op. No row is read,
rewritten or deleted.
"""

from __future__ import annotations

from alembic import op

revision = "0003_policy_engine"
down_revision = "0002_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE media_items
            ADD COLUMN IF NOT EXISTS reasons JSONB,
            ADD COLUMN IF NOT EXISTS policy_version TEXT,
            ADD COLUMN IF NOT EXISTS signals JSONB,
            ADD COLUMN IF NOT EXISTS policy_outcome TEXT
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            at TIMESTAMPTZ NOT NULL DEFAULT now(),
            kind VARCHAR(32) NOT NULL,
            item_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
            payload JSONB
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_at ON events (at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS events")
    op.execute(
        """
        ALTER TABLE media_items
            DROP COLUMN IF EXISTS policy_outcome,
            DROP COLUMN IF EXISTS signals,
            DROP COLUMN IF EXISTS policy_version,
            DROP COLUMN IF EXISTS reasons
        """
    )
