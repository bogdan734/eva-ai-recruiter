"""Single registry of vacancies we recruit for.

Everything that needs to say "which job did this person apply to" reads from
here: the job-board pullers, the intake router and the CRM card. Same idea as
`sources.py` — adding a vacancy means one entry below, not a hunt through
hardcoded ids.

Two kinds of route exist:

  - **full** (`calls_enabled=True`): the original Kozyr Trans pipeline. Geo and
    profile filters run, Єва calls and chats, the CRM card is written after the
    call by `_finalize_call`.

  - **intake-only** (`calls_enabled=False`): the puller drops a card into the
    vacancy's own KeyCRM funnel and stops. No geo filter, no profile filter, no
    call, no Telegram, no robota.ua chat — a recruiter works the card by hand.
    Added 2026-08-04 for «Бухгалтер (єдиний)» at the client's request.

The ids below are live and were verified against both cabinets on 2026-08-04.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Local FK into the `vacancies` table. The pullers have always written 1 here;
# the board's own vacancy id lives in the route and in the log line.
LOCAL_FK = 1


@dataclass(frozen=True)
class Vacancy:
    key: str                      # canonical short key used across the codebase
    label: str                    # KeyCRM «Вакансія» (LD_1001) select value
    workua_ids: frozenset[int]    # work.ua job ids that feed this vacancy
    robotaua_ids: frozenset[int]  # robota.ua vacancy ids that feed this vacancy
    keycrm_pipeline_id: int       # KeyCRM funnel the card is created in
    keycrm_status_id: int         # stage the card lands on
    calls_enabled: bool           # Єва calls / chats candidates of this vacancy
    screen_enabled: bool          # geo + profile + name filters run at intake
    vacancy_number: str = ""      # KeyCRM «Номер вакансії» (LD_1002)
    vacancy_url: str = ""         # KeyCRM «Посилання на вакансію» (LD_1004)
    open_paid_contacts: bool = False  # may spend robota.ua paid contact openings
    # Words that mark someone as plausibly right for this role, read off the free
    # part of a robota.ua record before any contact is opened. Empty means
    # "cannot judge": scoring then stays neutral rather than refusing everyone,
    # so a vacancy nobody has described yet still collects people.
    role_markers: tuple[str, ...] = ()
    # Do we create cards for this vacancy at all? Off means the pullers skip it
    # entirely — used when ANOTHER system already owns that funnel and our cards
    # would be duplicates. See ACCOUNTANT below.
    intake_enabled: bool = True

    # ---- what Єва says out loud, per vacancy -----------------------------
    # Empty means "fall back to the global .env value", so adding these changed
    # nothing for anyone until a vacancy fills them in. Before this the pitch was
    # a single global text and every candidate heard the same one, whichever
    # posting they had answered.
    #
    # ⚠️ These are SPOKEN, not read. Write numbers as words — "25000" comes out
    # as a string of digits — and avoid latin script, which the TTS pronounces in
    # English ("B2B" → write "бі-ту-бі"). Same rule as the .env values they
    # replace; see the pronunciation lessons in the handoff.
    spoken_title: str = ""        # посада, як Єва її називає
    spoken_salary: str = ""       # зарплата словами
    spoken_schedule: str = ""     # графік
    spoken_benefits: str = ""     # умови й переваги
    spoken_pitch: str = ""        # презентація компанії під цю вакансію


SALES = Vacancy(
    key="sales",
    label="Менеджер з продажу",
    workua_ids=frozenset({8249916}),
    robotaua_ids=frozenset({11277559, 11284462}),
    keycrm_pipeline_id=1,          # «1 Етап Менеджер з продажу»
    keycrm_status_id=1,            # «Новий» — real stage is set by the orchestrator
    calls_enabled=True,
    screen_enabled=True,
    vacancy_number="8249916",
    vacancy_url="https://www.work.ua/jobs/8249916/",
    open_paid_contacts=True,
    role_markers=(
        "логіст", "продаж", "менеджер", "sales", "logistic", "експедит", "закупів",
    ),
)

# Office roles in Dnipro (Барикадна, 15А), full-time. Єва is deliberately NOT
# wired to them: the client wants the responses in the funnel and handled by a
# human. Paid robota.ua contact openings stay off — we never spend quota on a
# vacancy nobody is going to call.
#
# intake_enabled: off on the morning of 2026-08-05, back ON the same evening.
#
# Why off: funnel 6 was being fed by another integration and our cards duplicated
# 19 of theirs; a recruiter renamed them «ЗАДВОЄНО» by hand.
# Why on again: that integration turned out not to route by vacancy at all — on
# 05.08 it put 20 of 23 new cards into funnel 1 «Менеджер з продажу», accountant
# applicants included (e.g. card 10041, Сарібекян, 13:30). So with us out of the
# way nobody was filling funnel 6 and the recruiter stopped seeing accountants.
#
# Duplicates are now acceptable BECAUSE they land in different funnels: theirs in
# 1, ours in 6, and the two are worked by different people. This depends on the
# per-funnel CRM dedup in InboundRouter — see find_lead_by_phone(pipeline_id=...).
# The real fix is still on their side: teach that connector to route by vacancy.
ACCOUNTANT = Vacancy(
    key="accountant",
    label="Бухгалтер",
    # Both accountant postings, per the client's 05.08 message. They resolve to
    # the same funnel; kept listed so routing is explicit if intake resumes.
    #   8242731 / 11249166 — Бухгалтер (єдиний)
    #   8374143 / 11292426 — Помічник бухгалтера, бухгалтер з первинної документації
    workua_ids=frozenset({8242731, 8374143}),
    robotaua_ids=frozenset({11249166, 11292426}),
    keycrm_pipeline_id=6,          # «Бухгалтер»
    keycrm_status_id=84,           # «Новий»
    calls_enabled=False,
    screen_enabled=False,
    vacancy_number="8242731",
    vacancy_url="https://www.work.ua/jobs/8242731/",
    open_paid_contacts=False,
    intake_enabled=True,
    # Both halves of «Помічник керівника/ бухгалтера». The bookkeeping words
    # alone refused «Помічник керівника» — literally half the posting's own
    # title, and one of the people the client pointed at when they asked why
    # robota.ua applicants were missing.
    role_markers=(
        "бухгалтер", "облік", "обліков", "фінанс", "економіст",
        "казначей", "первинн", "аудит", "податк", "1с",
        "помічник керівника", "асистент", "офіс-менеджер",
        "діловод", "документообіг", "оператор пк",
    ),
)

# Shipped in code. The live registry is `all_vacancies()` — these are only the
# starting point it merges panel edits over.
SHIPPED: dict[str, Vacancy] = {v.key: v for v in (SALES, ACCOUNTANT)}

DEFAULT = SALES


def all_vacancies() -> dict[str, Vacancy]:
    """Everything we recruit for right now: shipped defaults with the panel's
    edits applied, plus vacancies created from the panel.

    Read fresh every call rather than cached at import. The bot, the API and the
    scheduler are separate processes: a vacancy added in the bot has to be
    visible to the puller without restarting anything, and caching here is how
    you get a vacancy that exists in one container and not the others.
    """
    from src.common import vacancy_store  # local import: the store imports nothing from here

    out = {k: vacancy_store.apply(v) for k, v in SHIPPED.items()}
    out.update(vacancy_store.custom_vacancies(Vacancy))
    return out


def get(key: str | None) -> Vacancy:
    """Route by canonical key; unknown keys fall back to the original pipeline."""
    return all_vacancies().get((key or "").strip().lower(), DEFAULT)


def for_workua(job_id: int | str | None) -> Vacancy | None:
    """Which vacancy does this work.ua job id belong to? None = not ours."""
    try:
        jid = int(job_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for v in all_vacancies().values():
        if jid in v.workua_ids:
            return v
    return None


def for_robotaua(vacancy_id: int | str | None) -> Vacancy | None:
    """Which vacancy does this robota.ua vacancy id belong to? None = not ours."""
    try:
        vid = int(vacancy_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for v in all_vacancies().values():
        if vid in v.robotaua_ids:
            return v
    return None


def workua_ids(*, calls_only: bool = False) -> set[int]:
    """work.ua job ids we pull. `calls_only` keeps just the ones Єва works.

    A vacancy with intake_enabled=False is never pulled: another system owns
    that funnel and our card would be a duplicate.
    """
    out: set[int] = set()
    for v in all_vacancies().values():
        if not v.intake_enabled:
            continue
        if calls_only and not v.calls_enabled:
            continue
        out |= set(v.workua_ids)
    return out


def robotaua_ids(*, calls_only: bool = False) -> set[int]:
    """robota.ua vacancy ids we pull. `calls_only` keeps Єва's own.

    The chat poller passes calls_only=True — Єва must never open a conversation
    with someone who applied to an intake-only vacancy.
    """
    out: set[int] = set()
    for v in all_vacancies().values():
        if not v.intake_enabled:
            continue
        if calls_only and not v.calls_enabled:
            continue
        out |= set(v.robotaua_ids)
    return out


def intake_blocked(vacancy) -> bool:
    """True when this vacancy's cards belong to another system, not us."""
    return vacancy is not None and not vacancy.intake_enabled
