"""Give `candidates.source` room for more than one channel.

VARCHAR(32) fits exactly one tag. The router records every channel a person
reached us through, so the second one —
"workua_response_send,workua_response_phonecall", 45 characters — overflowed the
column and took the whole ingest transaction down with it. The work.ua catch-up
replay walked into this on 2026-08-18 and could not finish.

128 holds all four channels we have today with room to spare; `merge_sources()`
enforces the same ceiling in code so the write is bounded, not just the column.

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates ALTER COLUMN source TYPE VARCHAR(128)")


def downgrade() -> None:
    # Lossy by nature: rows holding more than one tag cannot fit back.
    op.execute("ALTER TABLE candidates ALTER COLUMN source TYPE VARCHAR(32)")
