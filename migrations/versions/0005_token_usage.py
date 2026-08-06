"""Track Anthropic tokens spent outside calls.

`Call.tokens_input/output` only ever carried the post-call summarizer's usage, so
the name-origin check (which runs on EVERY intake), the match scorer and the
Telegram userbot's prompts never reached `/costs`. This table collects them.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS to match 0004's habit: the live box may get this by hand first.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS token_usage (
            id            SERIAL PRIMARY KEY,
            date          VARCHAR(10) NOT NULL,
            component     VARCHAR(32) NOT NULL,
            model         VARCHAR(64) NOT NULL DEFAULT '',
            tokens_input  INTEGER NOT NULL DEFAULT 0,
            tokens_output INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_date ON token_usage (date)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_component ON token_usage (component)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_created_at ON token_usage (created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS token_usage")
