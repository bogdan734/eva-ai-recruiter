"""Remember which vacancy a candidate actually applied to.

`candidates.vacancy_id` has never carried this: `vacancies.LOCAL_FK = 1` means
every puller writes 1 there regardless of the posting the person answered. The
board's own job id decided the routing at intake and was then thrown away, so by
the time Eva dialled nobody could say which vacancy the call was about — which is
why the voice script is a single global text in `.env` and the same pitch is read
to everyone.

This column keeps the registry key (`sales`, `accountant`, ...) so the script,
the CRM funnel and the screening rules can differ per vacancy. NULL means "the
default vacancy", which is exactly how every existing row should be read: they
were all sales applicants.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS to match the habit of 0004/0005: the live box tends to get
    # schema by hand before the migration catches up.
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS vacancy_key VARCHAR(32)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_vacancy_key "
        "ON candidates (vacancy_key)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_candidates_vacancy_key")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS vacancy_key")
