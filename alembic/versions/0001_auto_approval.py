"""Auto-approval audit trail, parental-guide detail, and the two new statuses.

Revision ID: 0001_auto_approval
Revises:
Create Date: 2026-09-21

This is the first migration the project has ever had. The live schema was
built by SQLAlchemy's create_all and was never stamped, so this revision runs
against a database that already has every table. It therefore only ever adds
things, and every statement is guarded so a second run is a no-op:

  * ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so existing rows keep their data
    and get NULL in the new columns.
  * ALTER TYPE ... ADD VALUE IF NOT EXISTS for the new enum labels.
    PostgreSQL 12 and later allow this inside a transaction as long as the new
    label is not used in the same transaction, which it is not.

No row is read, rewritten or deleted.
"""

from __future__ import annotations

from alembic import op

revision = "0001_auto_approval"
down_revision = None
branch_labels = None
depends_on = None

# SQLAlchemy's Enum stores the *names* of the members, so the labels are the
# uppercase ones, matching the itemstatus type already in the database.
_NEW_STATUSES = ("AWAITING_DATA", "IGNORED")
_NEW_DOWNLOAD_STATUSES = ("CANCELLED",)


def upgrade() -> None:
    for label in _NEW_STATUSES:
        op.execute(f"ALTER TYPE itemstatus ADD VALUE IF NOT EXISTS '{label}'")
    for label in _NEW_DOWNLOAD_STATUSES:
        op.execute(f"ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'autodecision') THEN
                CREATE TYPE autodecision AS ENUM ('AUTO_APPROVE', 'NEEDS_REVIEW', 'WAIT');
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        ALTER TABLE media_items
            ADD COLUMN IF NOT EXISTS auto_decision autodecision,
            ADD COLUMN IF NOT EXISTS auto_reasons JSONB,
            ADD COLUMN IF NOT EXISTS auto_decided_at TIMESTAMPTZ
        """
    )

    op.execute(
        """
        ALTER TABLE content_checks
            ADD COLUMN IF NOT EXISTS alcohol_rating severityrating,
            ADD COLUMN IF NOT EXISTS guide_votes JSONB
        """
    )

    # FALSE is the safe default for every row written before this column
    # existed: rejecting one of those items leaves its torrent alone rather
    # than deleting files that may have been in the library all along.
    op.execute(
        """
        ALTER TABLE downloads
            ADD COLUMN IF NOT EXISTS created_torrent BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    # The columns come back out; the enum labels stay. PostgreSQL cannot drop a
    # single enum label, and dropping the type would take the status column
    # with it, so leaving the unused labels behind is the safe direction.
    #
    # Rolling the *image* back is the case that needs care, because the old
    # code cannot load a label it has never heard of. Move the rows first:
    #
    #   UPDATE media_items SET status = 'PENDING_REVIEW'
    #    WHERE status IN ('AWAITING_DATA', 'IGNORED');
    #   UPDATE downloads SET status = 'ERROR' WHERE status = 'CANCELLED';
    op.execute(
        """
        ALTER TABLE downloads
            DROP COLUMN IF EXISTS created_torrent
        """
    )
    op.execute(
        """
        ALTER TABLE content_checks
            DROP COLUMN IF EXISTS guide_votes,
            DROP COLUMN IF EXISTS alcohol_rating
        """
    )
    op.execute(
        """
        ALTER TABLE media_items
            DROP COLUMN IF EXISTS auto_decided_at,
            DROP COLUMN IF EXISTS auto_reasons,
            DROP COLUMN IF EXISTS auto_decision
        """
    )
    op.execute("DROP TYPE IF EXISTS autodecision")
