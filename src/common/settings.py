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

    telephony_provider: Literal["twilio", "ringostat", "plivo"] = "twilio"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
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

    # Pluggable job board providers (stubs — fill keys to enable).
    robotaua_api_token: str = ""
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
        "Київська,Житомирська,Вінницька,Хмельницька,Тернопільська,Львівська,"
        "Івано-Франківська,Закарпатська,Чернівецька,Рівненська,Волинська,Черкаська"
    )
    region_blacklist: str = (
        "м. Київ,Суми,Сумська,Запоріжжя,Запорізька,Херсон,Херсонська,Донецька"
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
    company_name: str = "Kozyr Trans"
    company_pitch: str = (
        "Наша компанія займається організацією вантажоперевезень як для приватних "
        "осіб, так і для підприємств."
    )
    default_vacancy_title: str = "Менеджер з продажу, логіст"
    default_vacancy_salary: str = (
        "30-65 тисяч гривень+ (залежить від навичок і результатів; "
        "на старті нижче під час навчання)"
    )
    default_vacancy_schedule: str = (
        "повністю віддалена, 5-денний робочий день з 9:00 до 17:00, сб-нд вихідні"
    )
    default_vacancy_benefits: str = (
        "навчання, підтримка кураторів, тепла база, ліди надходять щодня"
    )

    call_slots: str = "09:00,11:00,13:00,15:00,17:00,19:00"
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
