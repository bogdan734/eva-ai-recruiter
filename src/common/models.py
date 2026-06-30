from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CallStatus(str, Enum):
    SUCCESS = "success"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    HANGUP = "hangup"
    BLOCKED = "blocked"
    FAILED = "failed"


class CandidateStatus(str, Enum):
    NEW_RESUME = "new_resume"
    FILTERED = "filtered"
    IN_CALL_QUEUE = "in_call_queue"
    CALLING = "calling"
    UNREACHABLE = "unreachable"
    CALL_DONE = "call_done"
    MANAGER_REVIEW = "manager_review"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    CLOSED = "closed"


class Vacancy(Base):
    __tablename__ = "vacancies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keycrm_funnel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keycrm_lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone_e164: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    languages: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    work_ua_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[CandidateStatus] = mapped_column(String(32), default=CandidateStatus.NEW_RESUME.value)
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"), nullable=True)
    call_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    calls: Mapped[list[Call]] = relationship(back_populates="candidate")


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    vapi_call_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[CallStatus] = mapped_column(String(32), default=CallStatus.FAILED.value)
    audio_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    objections: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    language_used: Mapped[str | None] = mapped_column(String(8), nullable=True)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="calls")


class DailyCost(Base):
    __tablename__ = "daily_costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # YYYY-MM-DD
    claude_tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    claude_tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    claude_usd: Mapped[float] = mapped_column(Float, default=0.0)
    deepgram_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    deepgram_usd: Mapped[float] = mapped_column(Float, default=0.0)
    elevenlabs_chars: Mapped[int] = mapped_column(Integer, default=0)
    elevenlabs_usd: Mapped[float] = mapped_column(Float, default=0.0)
    vapi_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    vapi_usd: Mapped[float] = mapped_column(Float, default=0.0)
    telephony_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    telephony_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_usd: Mapped[float] = mapped_column(Float, default=0.0)
