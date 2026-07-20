"""create missing enum types + alter columns

Revision ID: 0002_enums
Revises: 0001_initial
Create Date: 2026-06-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_enums"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


CANDIDATE_VALUES = [
    "new_resume",
    "filtered",
    "in_call_queue",
    "calling",
    "unreachable",
    "call_done",
    "manager_review",
    "interview_scheduled",
    "closed",
]

CALL_VALUES = [
    "success",
    "no_answer",
    "busy",
    "voicemail",
    "hangup",
    "blocked",
    "failed",
]


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # Create enum types if missing
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'candidatestatus') "
        f"THEN CREATE TYPE candidatestatus AS ENUM ({_quoted(CANDIDATE_VALUES)}); END IF; END $$;"
    )
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'callstatus') "
        f"THEN CREATE TYPE callstatus AS ENUM ({_quoted(CALL_VALUES)}); END IF; END $$;"
    )
    # Convert existing VARCHAR columns to enum (if not already)
    op.execute(
        "ALTER TABLE candidates ALTER COLUMN status DROP DEFAULT;"
    )
    op.execute(
        "ALTER TABLE candidates ALTER COLUMN status TYPE candidatestatus "
        "USING status::candidatestatus;"
    )
    op.execute(
        "ALTER TABLE candidates ALTER COLUMN status SET DEFAULT 'new_resume'::candidatestatus;"
    )
    op.execute("ALTER TABLE calls ALTER COLUMN status DROP DEFAULT;")
    op.execute(
        "ALTER TABLE calls ALTER COLUMN status TYPE callstatus USING status::callstatus;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE candidates ALTER COLUMN status TYPE VARCHAR USING status::text;")
    op.execute("ALTER TABLE calls ALTER COLUMN status TYPE VARCHAR USING status::text;")
    op.execute("DROP TYPE IF EXISTS candidatestatus;")
    op.execute("DROP TYPE IF EXISTS callstatus;")
