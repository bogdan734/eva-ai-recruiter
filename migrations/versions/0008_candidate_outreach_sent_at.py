"""Remember that Eva wrote to someone instead of calling them.

The work.ua backfill of 2026-08-18 brought in 83 sales applicants nobody had
ever contacted — the postings they answered were deleted before our intake was
fixed. Calling them was ruled out: the vacancy is not on work.ua any more, so
83 outbound calls would be about a job the candidate cannot look up. They get a
Telegram message instead, at the userbot's own safe rate.

That rate is the reason for this column. Fifteen messages a day means the run
spans a week or more, so "who is still owed a message" has to survive restarts,
and it must never be answered by writing to someone twice.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS outreach_sent_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS outreach_sent_at")
