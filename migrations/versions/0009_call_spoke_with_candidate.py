"""Remember whether the candidate actually answered a screening question.

The post-call summarizer already judges this per call (`spoke_with_candidate`,
see src/call/summarizer.py) -- true only if the candidate answered at least one
real screening question, false for a greeting, a "can you hear me?" back-and-forth,
or dead air -- but the flag was only ever used in memory to decide the outcome
disposition and was never written to the row. The dispatcher's own
`REAL_CONTACT_SEC` guard (src/scheduler/dispatcher.py) had no way to see it, so
it fell back to a pure duration threshold: any call 40s or longer was treated as
"already had a real conversation, never redial". That is wrong for a call that
was 64 seconds of "алло, я вас не чую" with no screening question ever answered
(candidate Цвігун, id 3181, 03.08.2026) -- a long, empty call blocked her from
ever being called again despite an unspent attempt.

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE calls ADD COLUMN IF NOT EXISTS spoke_with_candidate BOOLEAN"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS spoke_with_candidate")
