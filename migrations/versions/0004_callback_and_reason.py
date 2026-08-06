"""Columns added live on 2026-07-22..27 that never made it into a migration.

SQLAlchemy's create_all() creates missing TABLES but never ALTERs existing ones, so
these three were applied by hand on the running VPS. A fresh install would come up
without them and crash on the first call — hence this catch-up migration.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS throughout: the live deployment already got these by hand, while a
    # fresh install has none of them. Both must survive this migration.
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS resume_text TEXT")
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS callback_at TIMESTAMPTZ")
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS ended_reason VARCHAR(64)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_callback_at "
        "ON candidates (callback_at)"
    )


def downgrade() -> None:
    op.drop_column("calls", "ended_reason")
    op.drop_index("ix_candidates_callback_at", table_name="candidates")
    op.drop_column("candidates", "callback_at")
    op.drop_column("candidates", "resume_text")
