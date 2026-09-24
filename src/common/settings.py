from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "staging", "prod"] = "dev"
    app_log_level: str = "INFO"
    app_timezone: str = "Europe/Kyiv"
    app_base_url: str = "https://api.recruiter-ai.example.com"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_model_cheap: str = "claude-haiku-4-5-20251001"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_language: str = "multi"
    deepgram_endpointing_ms: int = 300

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"

    vapi_api_key: str = ""
    vapi_assistant_id: str = ""
    vapi_webhook_secret: str = ""

    vapi_phone_number_id: str = ""  # Vapi phone-number id used for outbound (Stream Telecom trunk)
    # Vapi regions are isolated environments: an organization belongs to one,
    # and assistants, numbers and SIP credentials live with it. Moving to the
    # EU region is therefore a different base URL AND a different key -- kept
    # here so the move, and the move back, are one line in .env rather than an
    # edit to three source files while calls are in flight.
    #   US: https://api.vapi.ai    (sip.vapi.ai)
    #   EU: https://api.eu.vapi.ai (sip.eu.vapi.ai)
    vapi_base_url: str = "https://api.vapi.ai"
    ringostat_api_key: str = ""
    ringostat_project_id: str = ""

    keycrm_api_token: str = ""
    keycrm_funnel_id: int = 0
    keycrm_base_url: str = "https://openapi.keycrm.app/v1"
    keycrm_webhook_secret: str = ""

    workua_employer_email: str = ""
    workua_employer_password: str = ""
    workua_scrape_daily_limit: int = 50
    workua_proxy_url: str = ""
    workua_allowed_vacancy_ids: str = ""
    # Cold sourcing (search the work.ua resume database as a logged-in employer,
    # rather than waiting for candidates to respond). We never type the employer
    # password into an automated login -- a human logs in once in their own
    # browser and hands us the resulting session (Playwright storage_state JSON,
    # or a plain cookie export) instead. No session file -> the job logs once
    # and skips; it never crashes the scheduler and never blocks other polling.
    workua_session_state_path: str = "/state/workua_session.json"
    workua_cold_sourcing_enabled: bool = True
    # Resumes actually fed into Eva's call queue per run -- deliberately much
    # smaller than workua_scrape_daily_limit (which caps raw page fetches),
    # so one run can never flood the queue.
    workua_cold_sourcing_max_per_run: int = 10
    # Search result pages to open per role-marker query per vacancy, before
    # moving to the next query. Keeps one run's request count predictable.
    workua_cold_sourcing_max_per_query: int = 6
    # 07.09.2026: a resume's phone number is invisible until an employer
    # explicitly "opens contacts" on it -- this spends the account's SHARED
    # daily quota (seen live: "Ви можете відкрити 6 з 10 контактів, доступних
    # на день"), the same recruiter-facing limit used for browsing the site
    # by hand. Deliberately small and separate from the caps above, which
    # bound page fetches/queue size, not this specific paid/limited action --
    # leaves headroom for the recruiter's own manual use of the same account.
    # The account's daily contact allowance. Cold sourcing spends it in full:
    # unspent credits do not carry over, and every one of them is a candidate
    # Єва could have called.
    workua_max_contact_opens_per_run: int = 10
    # Source for cold sourcing. The API is the default because Cloudflare blocks
    # the resume-search pages from this host; the browser path stays available
    # for the day the API changes shape.
    workua_cold_sourcing_use_api: bool = True
    # How close a candidate must look to the vacancy to be worth a paid contact
    # open and a call. 0.65 was tuned against full resume pages from the
    # scraper; the API returns a few dozen words per person, which scores much
    # lower for the same human — at 0.65 the run of 17.09 rejected every single
    # candidate, including profiles literally listing «Менеджер з продажу».
    # Watch cold_sourcing.prescreen scores in the log before moving this again.
    cold_sourcing_match_threshold: float = 0.45
    # Search city by city across the hiring oblasts instead of nationwide.
    # Measured 18.09 on one query: 4 openable people nationwide (none of them
    # in our oblasts) against 37 across our own twelve cities.
    workua_cold_sourcing_by_region: bool = True
    # How deep to page each city. Three pages was tuned for a nationwide sweep;
    # a single city is a much smaller pool and only ~3% of resumes are openable
    # at all, so depth is where the reachable people are.
    workua_cold_sourcing_pages_per_query: int = 6
    # Hard ceiling on search requests per call, so a run that finds nobody still
    # ends: twelve cities times six pages times several queries would otherwise
    # walk for half an hour.
    workua_cold_sourcing_max_pages_per_run: int = 150
    # Human-pace pauses for cold sourcing specifically. The 1.5-4.5s used
    # elsewhere is fine for an API poller but reads as a bot when it is page
    # after page of a resume search.
    workua_cold_sourcing_min_delay_sec: float = 8.0
    workua_cold_sourcing_max_delay_sec: float = 20.0

    # Pluggable job board providers (stubs — fill keys to enable).
    robotaua_api_token: str = ""
    robotaua_employer_email: str = ""
    robotaua_employer_password: str = ""
    robotaua_allowed_vacancy_ids: str = ""
    jooble_api_key: str = ""
    olx_jobs_client_id: str = ""
    olx_jobs_client_secret: str = ""

    tg_report_bot_token: str = ""
    tg_report_chat_id: str = ""
    tg_report_hour: int = 9
    tg_report_minute: int = 0

    s3_endpoint: str = ""
    s3_bucket: str = "recruiter-ai-recordings"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "eu-central"

    database_url: str = "sqlite+aiosqlite:///./local.db"

    match_score_threshold: float = 0.65
    region_whitelist: str = (
        "Житомирська,Хмельницька,Тернопільська,Львівська,Івано-Франківська,"
        "Закарпатська,Чернівецька,Рівненська,Волинська,Черкаська,Одеська,"
        "Дніпропетровська"
    )
    region_blacklist: str = (
        "м. Київ,Київ,Київська,Вінниця,Вінницька,Суми,Сумська,"
        "Запоріжжя,Запорізька,Херсон,Херсонська,Донецька"
    )

    # Candidate profile filter (sales manager / logistics role)
    profile_age_min_f: int = 23
    profile_age_max_f: int = 42
    profile_age_min_m: int = 23
    profile_age_max_m: int = 40
    profile_age_min_with_edu: int = 22
    profile_required_country: str = "UA"
    profile_recent_role_years: int = 3
    profile_war_pause_year: int = 2022
    profile_war_pause_tolerance: int = 1

    # Persona + vacancy defaults (Kozyr Trans)
    agent_name: str = "Єва"
    tguserbot_url: str = "http://tguserbot:8090"
    # KeyCRM user assigned to every AI-processed lead so recruiters can
    # tell them apart in the list. 0 disables the assignment.
    keycrm_ai_manager_id: int = 7
    # Shared secret the userbot uses to reach the internal API endpoint.
    internal_api_token: str = "change-me-internal"
    # Vacancy link Eva sends at the start of a Telegram chat.
    vacancy_url: str = "https://www.work.ua/jobs/8249916/"
    # KeyCRM «Вакансія» select value + «Номер вакансії» — auto-filled on every card.
    keycrm_vacancy_label: str = "Менеджер з продажу"
    vacancy_number: str = "8249916"
    # Resume link in the card: "workua" = work.ua employer-cabinet applicant link
    # (default); "selfhosted" = our own {APP_BASE_URL}/resume/{id} page rendered from
    # the stored resume text (works without a work.ua login; used for robota.ua too).
    resume_link_mode: str = "workua"
    company_name: str = "Козир Транс"  # Cyrillic: the TTS reads latin letters in English
    company_pitch: str = (
        "Ми займаємося організацією внутрішніх та міжнародних вантажоперевезень."
    )
    default_vacancy_title: str = "Менеджер з продажу логістики B2B"
    default_vacancy_salary: str = (
        "від 30 до 65 тисяч гривень та більше"
    )
    default_vacancy_schedule: str = (
        "повністю віддалена, 5-денний робочий день з 9:00 до 17:00, сб-нд вихідні"
    )
    default_vacancy_benefits: str = (
        "навчання, підтримка кураторів, тепла база, ліди надходять щодня"
    )

    call_slots: str = "10:00,14:00,18:30"
    call_max_attempts: int = 3
    call_max_concurrent: int = 3
    call_max_duration_sec: int = 420

    guardrail_max_repetition: int = 2
    guardrail_max_forbidden_topic: int = 1

    @property
    def regions_allowed(self) -> set[str]:
        return {r.strip() for r in self.region_whitelist.split(",") if r.strip()}

    @property
    def regions_blocked(self) -> set[str]:
        return {r.strip() for r in self.region_blacklist.split(",") if r.strip()}

    @property
    def call_slot_times(self) -> list[str]:
        return [s.strip() for s in self.call_slots.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
