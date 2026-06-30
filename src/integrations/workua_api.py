"""work.ua official REST API client.

Docs (provided by partner team 2026-06-22):
- Base URL: https://api.work.ua/
- Auth: HTTP Basic (employer email + password)
- Rate limit: 50 RPS, IP blocked for 1 min on 429
- 401 -> bad creds; 403 -> account blocked; 429 -> rate limit; 501 -> wrong method

NO separate API key — uses employer login. Keep credentials in sops-encrypted .env.

⚠️ CRITICAL: `/resumes` and `/resume` endpoints CONSUME paid contact-opening credits.
Use them sparingly. `/jobs/responses` is FREE — that's our primary inbound source.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from src.common.settings import get_settings

log = structlog.get_logger()


class WorkUaAuthError(RuntimeError):
    """401/403 — auth failed or account blocked."""


class WorkUaRateLimitError(RuntimeError):
    """429 — rate limit or login lockout."""


class WorkUaApiError(RuntimeError):
    """Generic 4xx/5xx from work.ua."""


@dataclass
class WorkUaResponse:
    """Single response (відгук) — what a candidate sent to one of our vacancies."""
    id: int
    job_id: int | None
    candidate_id: int
    date: datetime | None
    fio: str | None
    from_type: str  # "send" | "phonecall"
    birth_date: str | None
    email: str | None
    phone: str | None
    type: str | None  # "resume" | "file" | "easy"
    with_file: bool
    text: str | None
    cover: str | None
    prefer_channels: list[str]


@dataclass
class WorkUaResume:
    """Resume detail (resume_id required, costs a paid contact opening)."""
    resume_id: int
    first_name: str | None
    last_name: str | None
    name: str | None  # job title in resume
    phone: str | None
    email: str | None
    region: str | None
    birth_date: str | None
    salary: int | None
    sex_rid: str | None
    experiences: list[dict[str, Any]]
    raw: dict[str, Any]


class WorkUaClient:
    BASE = "https://api.work.ua"

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        company_name: str | None = None,
        contact_email: str | None = None,
    ) -> None:
        s = get_settings()
        self._email = email or s.workua_employer_email
        self._password = password or s.workua_employer_password
        company = company_name or s.company_name or "AI Recruiter"
        contact = contact_email or self._email
        ua = f"{company} ({contact})"
        self._client = httpx.AsyncClient(
            base_url=self.BASE,
            auth=(self._email, self._password) if self._email else None,
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
                "X-Locale": "uk_UA",
            },
            timeout=httpx.Timeout(20.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _raise_for_status(self, r: httpx.Response, context: str) -> None:
        if r.status_code == 401:
            raise WorkUaAuthError(f"{context}: bad credentials")
        if r.status_code == 403:
            raise WorkUaAuthError(f"{context}: account blocked (contact work.ua manager)")
        if r.status_code == 429:
            raise WorkUaRateLimitError(f"{context}: 429 too many requests")
        if r.status_code >= 400:
            raise WorkUaApiError(f"{context}: HTTP {r.status_code} — {r.text[:200]}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        retry=retry_if_exception_type(WorkUaRateLimitError),
    )
    async def get_dictionaries(self) -> dict[str, Any]:
        """Reference data: town, category, jobtype, experience, education, etc.

        Cache locally — these change rarely. Used for vacancy creation + filter mapping.
        """
        r = await self._client.get("/dictionaries")
        self._raise_for_status(r, "GET /dictionaries")
        return r.json()

    async def get_dictionary(self, name: str) -> Any:
        r = await self._client.get(f"/dictionaries/{name}")
        self._raise_for_status(r, f"GET /dictionaries/{name}")
        return r.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        retry=retry_if_exception_type(WorkUaRateLimitError),
    )
    async def list_my_vacancies(
        self, *, full: bool = True, active: bool | None = None, page: int = 1, limit: int = 50
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": limit}
        if full:
            params["full"] = 1
        if active is not None:
            params["active"] = 1 if active else 0
        r = await self._client.get("/jobs/my", params=params)
        self._raise_for_status(r, "GET /jobs/my")
        return r.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        retry=retry_if_exception_type(WorkUaRateLimitError),
    )
    async def list_responses(
        self,
        *,
        limit: int = 50,
        last_id: int | None = None,
        before_id: int | None = None,
        before_ts: int | None = None,
        sort: int = 0,
        from_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """All responses across all our vacancies.

        last_id is the cursor: store the highest id we've processed and pass it next
        time to get only newer ones. By default returns only `send` responses; pass
        from_types=["send", "phonecall"] for both.
        """
        params: dict[str, Any] = {"limit": limit, "sort": sort}
        if last_id is not None:
            params["last_id"] = last_id
        if before_id is not None:
            params["before_id"] = before_id
        if before_ts is not None:
            params["before"] = before_ts
        if from_types:
            params["from_type[]"] = from_types
        r = await self._client.get("/jobs/responses/", params=params)
        if r.status_code == 404:
            return {"status": "ok", "items": []}
        self._raise_for_status(r, "GET /jobs/responses")
        return r.json()

    async def list_responses_for_vacancy(
        self, job_id: int, *, limit: int = 50, last_id: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if last_id is not None:
            params["last_id"] = last_id
        r = await self._client.get(f"/jobs/{job_id}/responses/", params=params)
        if r.status_code == 404:
            return {"status": "ok", "items": []}
        self._raise_for_status(r, f"GET /jobs/{job_id}/responses")
        return r.json()

    async def search_resumes(
        self,
        *,
        search: str,
        region_id: int | None = None,
        age_from: int | None = None,
        age_to: int | None = None,
        salary_from: int | None = None,
        salary_to: int | None = None,
        with_phone: bool = True,
        category_ids: list[int] | None = None,
        experience_ids: list[int] | None = None,
        education_ids: list[int] | None = None,
        employment_ids: list[int] | None = None,
        gender_ids: list[int] | None = None,
        period: int = 3,
        sort: int = 1,
        page: int = 1,
        limit: int = 20,
        anyword: bool = False,
    ) -> dict[str, Any]:
        """⚠️ COSTS PAID CREDITS — opens contacts on every match.

        Use after we've already qualified the candidate with profile filter via
        public/free signals (resume URL from response). Period: 1=1d, 2=7d, 3=30d,
        4=3mo, 5=1yr, 6=archive.
        """
        params: dict[str, Any] = {
            "search": search,
            "anyword": 1 if anyword else 0,
            "page": page,
            "limit": limit,
            "sort": sort,
            "period": period,
            "phone": 1 if with_phone else 0,
        }
        if region_id is not None:
            params["region[]"] = [region_id]
        if age_from is not None:
            params["agefrom"] = age_from
        if age_to is not None:
            params["ageto"] = age_to
        if salary_from is not None:
            params["salaryfrom"] = salary_from
        if salary_to is not None:
            params["salaryto"] = salary_to
        if category_ids:
            params["category[]"] = category_ids
        if experience_ids:
            params["experience[]"] = experience_ids
        if education_ids:
            params["education[]"] = education_ids
        if employment_ids:
            params["employment[]"] = employment_ids
        if gender_ids:
            params["gender[]"] = gender_ids
        r = await self._client.get("/resumes", params=params)
        self._raise_for_status(r, "GET /resumes")
        return r.json()

    async def get_resume(self, resume_id: int) -> dict[str, Any]:
        """⚠️ COSTS PAID CREDITS."""
        r = await self._client.get("/resume", params={"resume_id": resume_id})
        if r.status_code == 404:
            return {}
        self._raise_for_status(r, f"GET /resume?resume_id={resume_id}")
        return r.json()

    async def available_publications(self) -> list[dict[str, Any]]:
        r = await self._client.get("/available-publications")
        self._raise_for_status(r, "GET /available-publications")
        data = r.json()
        return data if isinstance(data, list) else []


def parse_response(raw: dict[str, Any]) -> WorkUaResponse:
    date_str = raw.get("date")
    parsed_date = None
    if date_str:
        try:
            parsed_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            parsed_date = None
    return WorkUaResponse(
        id=int(raw["id"]),
        job_id=int(raw["job_id"]) if raw.get("job_id") else None,
        candidate_id=int(raw["candidate_id"]) if raw.get("candidate_id") else 0,
        date=parsed_date,
        fio=raw.get("fio"),
        from_type=raw.get("from_type", "send"),
        birth_date=raw.get("birth_date"),
        email=raw.get("email"),
        phone=raw.get("phone"),
        type=raw.get("type"),
        with_file=bool(int(raw.get("with_file") or 0)),
        text=raw.get("text"),
        cover=raw.get("cover"),
        prefer_channels=list(raw.get("preferCommunicationChannels") or []),
    )


def parse_resume(raw: dict[str, Any]) -> WorkUaResume:
    result = raw.get("result") or raw
    contacts = result.get("contacts") or {}
    return WorkUaResume(
        resume_id=int(result.get("resume_id") or result.get("id") or 0),
        first_name=result.get("first_name"),
        last_name=result.get("last_name"),
        name=result.get("name"),
        phone=contacts.get("phone") or result.get("phone"),
        email=contacts.get("email") or result.get("email"),
        region=result.get("region"),
        birth_date=result.get("birth_date"),
        salary=int(result["salary"]) if result.get("salary") else None,
        sex_rid=result.get("sex_rid"),
        experiences=contacts.get("exprns") or result.get("exprns") or [],
        raw=result,
    )
