"""Call FSM — Kozyr Trans flow (Єва-script v1).

11 explicit steps matching docs/call_script_v1_kozyr_trans.md. Each step has a
single conversational purpose; the orchestrator advances on tool-call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Step(IntEnum):
    GREETING = 1               # привітання + згода на запис
    CONFIRM_INTENT = 2         # підтвердження work.ua + пошуку sales/logist
    REGION_CHECK = 3           # уточнення регіону
    PITCH = 4                  # пітч вакансії + перше питання про досвід
    BEHAVIORAL = 5             # 3 поведінкові питання
    MOTIVATION = 6             # мотивація змінити роботу
    SALARY = 7                 # ЗП-очікування + скрипт-відповідь 30-65k+
    SCHEDULE = 8               # графік + віддалена робота
    TECH = 9                   # ноутбук + гарнітура
    INTEREST = 10              # підтвердження зацікавленості
    HANDOFF = 11               # передача менеджеру + прощання


@dataclass
class CallState:
    step: Step = Step.GREETING
    language: str = "uk"
    consent_given: bool | None = None

    # Step 2-3
    confirmed_intent: bool | None = None
    confirmed_region: bool | None = None

    # Step 4-5 (qualification deep)
    sales_experience_years: int | None = None
    sales_type: str | None = None  # "phone"|"direct"|"b2b"|"retail"|"none"
    has_logistics_experience: bool | None = None
    behavioral_notes: list[str] = field(default_factory=list)

    # Step 6
    motivation: str | None = None

    # Step 7
    candidate_salary_expectation: str | None = None
    salary_script_delivered: bool = False

    # Step 8
    remote_work_ok: bool | None = None
    schedule_ok: bool | None = None

    # Step 9
    has_laptop_or_pc: bool | None = None
    has_headset: bool | None = None

    # Step 10
    candidate_interested: bool | None = None

    # Cross-cutting
    objections_raised: list[str] = field(default_factory=list)
    qualified: bool | None = None
    callback_at: str | None = None
    exit_reason: str | None = None

    def advance(self) -> Step:
        if self.step < Step.HANDOFF:
            self.step = Step(self.step + 1)
        return self.step

    def to_dict(self) -> dict[str, object]:
        return {
            "step": int(self.step),
            "language": self.language,
            "consent_given": self.consent_given,
            "confirmed_intent": self.confirmed_intent,
            "confirmed_region": self.confirmed_region,
            "sales_experience_years": self.sales_experience_years,
            "sales_type": self.sales_type,
            "has_logistics_experience": self.has_logistics_experience,
            "behavioral_notes": list(self.behavioral_notes),
            "motivation": self.motivation,
            "candidate_salary_expectation": self.candidate_salary_expectation,
            "salary_script_delivered": self.salary_script_delivered,
            "remote_work_ok": self.remote_work_ok,
            "schedule_ok": self.schedule_ok,
            "has_laptop_or_pc": self.has_laptop_or_pc,
            "has_headset": self.has_headset,
            "candidate_interested": self.candidate_interested,
            "objections_raised": list(self.objections_raised),
            "qualified": self.qualified,
            "callback_at": self.callback_at,
            "exit_reason": self.exit_reason,
        }
