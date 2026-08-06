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

    vapi_phone_number_id: str = ""  # Vapi phone-number id used for outbound (Zadarma trunk)
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
    company_name: str = "Kozyr Trans"
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
