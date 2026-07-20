"""Hard pre-filters for the Sales Manager / Logistics candidate profile.

Runs BEFORE expensive embedding match. Cheap text-based checks that auto-reject
candidates outside the demographic / role / experience window defined in
docs/candidate_profile.md.

Updated 2026-06-20 per client clarifications:
  Q1. Age 22 allowed when profile education present
  Q2. Profile education = logistics OR sales (incl. adjacent: mgmt/marketing/economics)
  Q3. Past self-employed who switched to sales = OK (talks on call)
  Q4. Recent-role rule: last 3 years must contain sales-manager-grade role
       Exception: resume gaps ending ~2022 (war pause) are acceptable
  Q5. Unclear sales-type in resume = pass pre-filter, AI asks on call
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from src.common.regions import is_region_allowed, normalize_region
from src.common.settings import get_settings


class Gender(str, Enum):
    FEMALE = "F"
    MALE = "M"
    UNKNOWN = "?"


# Positive title markers — at least one must appear in desired_position OR last_position
POSITIVE_TITLES = [
    "менеджер з продаж",
    "менеджер по продаж",
    "менеджер продаж",
    "sales manager",
    "sales rep",
    "sales representative",
    "account manager",
    "account executive",
    "телефонн",  # телефонні продажі
    "активн",  # активні продажі
    "холодн",  # холодні дзвінки
    "b2b",
    "логіст",
    "logistics",
    "expediter",
    "експедит",
    "fulfillment",
    "supply chain",
    "ланцюг постачання",
    "продаж",
]

# Strong "manager-grade" markers — at least one must be in the RECENT (last N yr) experience
RECENT_MANAGER_MARKERS = [
    "менеджер з продаж",
    "менеджер по продаж",
    "менеджер продаж",
    "sales manager",
    "account manager",
    "account executive",
    "менеджер з логіст",
    "логіст-менеджер",
    "менеджер з холодних",
    "менеджер з телефонних",
    "head of sales",
    "керівник відділу продаж",
    "team lead sales",
]

# Negative title markers — if present in last_position, auto-reject
# (UNLESS overridden by a strong positive marker elsewhere)
NEGATIVE_TITLES = [
    "касир",
    "cashier",
    "продавець",
    "продавец",
    "торгов",  # торговий представник
    "merchandiser",
    "мерчендайзер",
    "фармацев",
    "провізор",
    "провизор",
    "лікар",
    "врач",
    "медсестра",
    "медбрат",
    "аптек",
    "майстер манікюру",
    "майстер педикюру",
    "майстер вій",
    "майстер брів",
    "майстер з нарощування",
    "масаж",
    "косметол",
    "стиліст",
    "перукар",
    "візаж",
]

# Profile education markers — Q2 clarified: logistics / sales / mgmt / marketing / economics
EDUCATION_PROFILE_MARKERS = [
    "логіст",
    "продаж",
    "sales",
    "marketing",
    "маркетинг",
    "менеджмент",
    "менеджер",
    "management",
    "економік",
    "economics",
    "комерц",
    "supply chain",
    "ланцюг постачання",
    "торговельн",
    "торгівл",
]


@dataclass
class FilterResult:
    accepted: bool
    reason: str = ""
    matched_positive: list[str] | None = None
    matched_negative: list[str] | None = None
    has_profile_edu: bool = False
    recent_manager_role: bool | None = None  # None = unknown, True/False = determined
    war_pause_exception: bool = False
    diagnostics: dict[str, str | int | bool] = field(default_factory=dict)


def _lower(s: str | None) -> str:
    return (s or "").lower()


def _contains_any(text: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n in text]


def _calc_age(birth_year: int | None) -> int | None:
    if not birth_year:
        return None
    today = date.today()
    return today.year - birth_year


def has_profile_education(education_text: str | None) -> bool:
    """Q2: profile = logistics + sales (incl. adjacent: mgmt/marketing/econ)."""
    if not education_text:
        return False
    hay = _lower(education_text)
    return bool(_contains_any(hay, EDUCATION_PROFILE_MARKERS))


def check_age(age: int | None, gender: Gender, has_edu: bool) -> tuple[bool, str]:
    """Q1: 22 floor if has_edu else 23 floor; upper bound per gender."""
    s = get_settings()
    if age is None:
        return True, "age_unknown"  # don't reject on missing data, let AI ask

    if gender == Gender.FEMALE:
        upper = s.profile_age_max_f
        lower = s.profile_age_min_with_edu if has_edu else s.profile_age_min_f
    elif gender == Gender.MALE:
        upper = s.profile_age_max_m
        lower = s.profile_age_min_with_edu if has_edu else s.profile_age_min_m
    else:
        # widest possible window when gender unknown
        upper = max(s.profile_age_max_f, s.profile_age_max_m)
        lower = s.profile_age_min_with_edu if has_edu else min(s.profile_age_min_f, s.profile_age_min_m)

    ok = lower <= age <= upper
    return ok, f"age={age} gender={gender.value} window=[{lower}-{upper}] edu={has_edu}"


def check_region(region: str | None) -> tuple[bool, str]:
    s = get_settings()
    if not region:
        return True, "region_unknown"  # let AI ask on call
    norm = normalize_region(region)
    ok = is_region_allowed(norm, s.regions_allowed, s.regions_blocked)
    return ok, f"region={norm}"


_BIRTH_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")
_DATE_RANGE_RE = re.compile(
    r"\b(?:з|с|from)?\s*(\d{1,2}[./]\d{4}|\d{4})\s*(?:-|—|–|до|до тепер|по|по тепер|present|now|тепер)?\s*"
    r"(\d{1,2}[./]\d{4}|\d{4}|тепер|present|now|по теперішній час)?",
    re.IGNORECASE,
)


def extract_birth_year(text: str) -> int | None:
    """Heuristic: look for a 4-digit year that could be a birth year (1950-2009)."""
    matches = _BIRTH_YEAR_RE.findall(text or "")
    plausible = [int(m) for m in matches if 1950 <= int(m) <= date.today().year - 18]
    if not plausible:
        return None
    return min(plausible)  # earliest plausible year is typically birth year


def _extract_year(token: str) -> int | None:
    if not token:
        return None
    m = re.search(r"(19[5-9]\d|20[0-3]\d)", token)
    return int(m.group(1)) if m else None


@dataclass
class WorkPeriod:
    start_year: int | None
    end_year: int | None  # None = "present"
    role_text: str


def parse_work_periods(experience_text: str | None) -> list[WorkPeriod]:
    """Scan free-text experience block for "YYYY - YYYY|present" + nearby role text.

    Resume formats vary wildly. This is best-effort, NOT a full parser. Use the
    structured WorkUa API field if available; this is fallback.
    """
    if not experience_text:
        return []
    periods: list[WorkPeriod] = []
    text = experience_text.replace("\r\n", "\n")
    lines = text.split("\n")
    today = date.today()

    range_re = re.compile(
        r"(?P<start>\d{2}[./]\d{4}|\d{4})\s*(?:-|—|–|до|по|until)\s*"
        r"(?P<end>\d{2}[./]\d{4}|\d{4}|тепер|present|now|теперіш|по теперішній час|поточний)",
        re.IGNORECASE,
    )
    for idx, raw_line in enumerate(lines):
        for m in range_re.finditer(raw_line):
            start = _extract_year(m.group("start"))
            end_token = m.group("end")
            end: int | None
            if end_token and re.search(r"\d{4}", end_token):
                end = _extract_year(end_token)
            else:
                # "present" / "тепер" → still working
                end = today.year if end_token else None
            # Role text is typically on the same line or the previous one
            role_text_candidates = [raw_line]
            if idx > 0:
                role_text_candidates.insert(0, lines[idx - 1])
            role_text = " ".join(rc.strip() for rc in role_text_candidates if rc.strip())
            periods.append(WorkPeriod(start_year=start, end_year=end, role_text=role_text))
    return periods


def check_recent_role(
    experience_text: str | None,
    *,
    recent_years: int,
    war_pause_year: int,
    war_pause_tolerance: int,
) -> tuple[bool | None, dict[str, str | int | bool]]:
    """Q4 rule: in the last `recent_years`, must contain a manager-grade sales role.

    Returns (passed, diagnostics). If experience_text is empty or unparseable,
    returns (None, {...}) — meaning "unknown, let AI verify on call".

    War-pause exception: if the latest period ends within [war_pause_year ± tolerance]
    AND that period was a qualifying role → pass (treat the gap since as war-related).
    """
    diag: dict[str, str | int | bool] = {}
    periods = parse_work_periods(experience_text)
    if not periods:
        diag["periods_found"] = 0
        return None, diag

    today_year = date.today().year
    cutoff = today_year - recent_years
    diag["periods_found"] = len(periods)
    diag["cutoff_year"] = cutoff

    has_recent_manager = False
    latest_qualifying_end: int | None = None
    for p in periods:
        if p.end_year is None:
            continue
        role_l = _lower(p.role_text)
        is_manager = bool(_contains_any(role_l, RECENT_MANAGER_MARKERS))
        in_recent = p.end_year >= cutoff
        if is_manager and in_recent:
            has_recent_manager = True
        if is_manager:
            latest_qualifying_end = max(latest_qualifying_end or 0, p.end_year)

    diag["has_recent_manager"] = has_recent_manager
    diag["latest_qualifying_end"] = latest_qualifying_end or 0

    if has_recent_manager:
        return True, diag

    # War-pause exception
    if latest_qualifying_end and abs(latest_qualifying_end - war_pause_year) <= war_pause_tolerance:
        diag["war_pause_exception"] = True
        return True, diag

    return False, diag


def detect_gender_from_name(full_name: str | None) -> Gender:
    """Cheap heuristic on UA/RU name endings. Far from perfect, but good enough
    as a hint when the resume doesn't specify gender."""
    if not full_name:
        return Gender.UNKNOWN
    first = full_name.strip().split()[0].lower() if full_name.strip() else ""
    if first.endswith(("а", "я", "ія", "на")) and not first.endswith(("ка", "ча")):
        return Gender.FEMALE
    if first.endswith(("о", "ій", "ко", "ук", "юк", "ук", "ев", "ов", "ін", "ин")):
        return Gender.MALE
    return Gender.UNKNOWN


def evaluate(
    *,
    full_name: str | None,
    region: str | None,
    desired_position: str | None,
    last_position: str | None = None,
    resume_text: str | None = None,
    experience_text: str | None = None,
    education_text: str | None = None,
    birth_year: int | None = None,
    gender: Gender | None = None,
    country: str | None = None,
) -> FilterResult:
    """Apply all hard filters in order. Return first failure or success."""
    s = get_settings()

    # 0. Country
    if country and country.upper() != s.profile_required_country:
        return FilterResult(
            accepted=False,
            reason=f"country={country} (required {s.profile_required_country})",
        )

    # 1. Region
    region_ok, region_reason = check_region(region)
    if not region_ok:
        return FilterResult(accepted=False, reason=region_reason)

    # 2. Education (used by age check)
    has_edu = has_profile_education(education_text) or has_profile_education(resume_text)

    # 3. Age
    g = gender or detect_gender_from_name(full_name)
    if birth_year is None:
        for src in (resume_text, experience_text):
            if not birth_year and src:
                birth_year = extract_birth_year(src)
    age = _calc_age(birth_year)
    age_ok, age_reason = check_age(age, g, has_edu)
    if not age_ok:
        return FilterResult(
            accepted=False, reason=age_reason, has_profile_edu=has_edu
        )

    # 4. Title / role check (initial)
    haystack = " ".join(
        _lower(p) for p in [desired_position, last_position, experience_text, resume_text] if p
    )
    positives = _contains_any(haystack, POSITIVE_TITLES)
    negatives = _contains_any(haystack, NEGATIVE_TITLES)

    last_position_l = _lower(last_position)
    last_negs = _contains_any(last_position_l, NEGATIVE_TITLES)
    last_pos = _contains_any(last_position_l, POSITIVE_TITLES)
    if last_negs and not last_pos:
        return FilterResult(
            accepted=False,
            reason=f"last_position negative: {last_negs}",
            matched_negative=last_negs,
            has_profile_edu=has_edu,
        )

    if not positives:
        return FilterResult(
            accepted=False,
            reason="no positive role marker (sales/logistics)",
            matched_negative=negatives or None,
            has_profile_edu=has_edu,
        )

    # 5. Recent-role (last 3 years) check — Q4
    recent_ok, recent_diag = check_recent_role(
        experience_text,
        recent_years=s.profile_recent_role_years,
        war_pause_year=s.profile_war_pause_year,
        war_pause_tolerance=s.profile_war_pause_tolerance,
    )
    if recent_ok is False:
        return FilterResult(
            accepted=False,
            reason=f"no manager-grade sales role in last {s.profile_recent_role_years}y",
            matched_positive=positives,
            matched_negative=negatives or None,
            has_profile_edu=has_edu,
            recent_manager_role=False,
            diagnostics=recent_diag,
        )

    war_exc = bool(recent_diag.get("war_pause_exception", False))
    return FilterResult(
        accepted=True,
        reason="ok",
        matched_positive=positives,
        matched_negative=negatives or None,
        has_profile_edu=has_edu,
        recent_manager_role=recent_ok,
        war_pause_exception=war_exc,
        diagnostics=recent_diag,
    )
