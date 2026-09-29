"""What Eva did in Telegram, for the lead card.

29.09.2026: recruiters could see how many times Eva called and whether she wrote
only on a saved buyer's card. The lead card now opens «AI Summary» with that line,
and the Telegram half of it needs a place to live: `outreach_sent_at` is also set
for people the outreach walker skipped, so it cannot say "she wrote".

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS tg_note VARCHAR(80)")


def downgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS tg_note")
