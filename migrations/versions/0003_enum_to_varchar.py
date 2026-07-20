"""revert enum columns to varchar (SQLAlchemy stores enum.name as string)

Revision ID: 0003_enum_to_varchar
Revises: 0002_enums
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op

revision = "0003_enum_to_varchar"
down_revision = "0002_enums"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates ALTER COLUMN status DROP DEFAULT;")
    op.execute("ALTER TABLE candidates ALTER COLUMN status TYPE VARCHAR(32) USING status::text;")
    op.execute("ALTER TABLE candidates ALTER COLUMN status SET DEFAULT 'NEW_RESUME';")
    op.execute("ALTER TABLE calls ALTER COLUMN status TYPE VARCHAR(32) USING status::text;")
    op.execute("DROP TYPE IF EXISTS candidatestatus;")
    op.execute("DROP TYPE IF EXISTS callstatus;")


def downgrade() -> None:
    pass
