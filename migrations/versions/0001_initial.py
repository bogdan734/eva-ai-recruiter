"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-20 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vacancies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keycrm_funnel_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keycrm_lead_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False, unique=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("desired_position", sa.String(length=255), nullable=True),
        sa.Column("experience_years", sa.Integer(), nullable=True),
        sa.Column("languages", sa.JSON(), nullable=True),
        sa.Column("work_ua_url", sa.String(length=512), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new_resume"),
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), nullable=True),
        sa.Column("call_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_candidates_phone_e164", "candidates", ["phone_e164"], unique=True)
    op.create_index("ix_candidates_keycrm_lead_id", "candidates", ["keycrm_lead_id"])

    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("vapi_call_id", sa.String(length=128), nullable=True, unique=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="failed"),
        sa.Column("audio_url", sa.String(length=512), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(length=16), nullable=True),
        sa.Column("objections", sa.JSON(), nullable=True),
        sa.Column("language_used", sa.String(length=8), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tags", sa.JSON(), nullable=True),
    )

    op.create_table(
        "daily_costs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.String(length=10), nullable=False, unique=True),
        sa.Column("claude_tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claude_tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claude_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("deepgram_minutes", sa.Float(), nullable=False, server_default="0"),
        sa.Column("deepgram_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("elevenlabs_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elevenlabs_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("vapi_minutes", sa.Float(), nullable=False, server_default="0"),
        sa.Column("vapi_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("telephony_minutes", sa.Float(), nullable=False, server_default="0"),
        sa.Column("telephony_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_daily_costs_date", "daily_costs", ["date"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_daily_costs_date", table_name="daily_costs")
    op.drop_table("daily_costs")
    op.drop_table("calls")
    op.drop_index("ix_candidates_keycrm_lead_id", table_name="candidates")
    op.drop_index("ix_candidates_phone_e164", table_name="candidates")
    op.drop_table("candidates")
    op.drop_table("vacancies")
