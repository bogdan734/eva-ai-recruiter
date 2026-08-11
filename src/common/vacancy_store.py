"""Vacancies a recruiter can edit and create without a deploy.

`vacancies.py` holds the shipped defaults. Anything changed or added from the
Telegram panel lands here — a JSON file on the mounted state volume, merged over
those defaults on read.

Why not `.env`: it has one value per setting for the whole system, which is
exactly why every candidate used to hear the same pitch whichever posting they
answered. Why not writing back into `vacancies.py`: code is baked into the image
at build time, so a panel edit would vanish on the next `docker compose build` —
the trap that ate the work.ua cursor for weeks.

Two kinds of field, and the difference is not cosmetic:

  **spoken** — what Єва says out loud. Getting it wrong sounds bad on one call.

  **operational** — board ids, CRM funnel, whether we call and whether we screen.
  Getting these wrong routes real people into the wrong funnel, or starts
  dialling candidates nobody meant to dial. They are editable because the client
  asked for it, but every one is validated on the way in, and the dangerous ones
  are typed rather than free text.

Writes are atomic through a temp file: a half-written store parses as "no edits"
and would silently drop every vacancy back to the shipped defaults.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from src.common.state import state_dir

log = structlog.get_logger()

STORE_NAME = "vacancy_overrides.json"

# --- what Єва says -----------------------------------------------------------
EDITABLE = ("spoken_title", "spoken_salary", "spoken_schedule", "spoken_benefits", "spoken_pitch")

LABELS = {
    "spoken_title": "Посада",
    "spoken_salary": "Зарплата",
    "spoken_schedule": "Графік",
    "spoken_benefits": "Умови",
    "spoken_pitch": "Про компанію",
}

# --- how the vacancy is worked ----------------------------------------------
OPS_FIELDS = (
    "label",
    "workua_ids",
    "robotaua_ids",
    "keycrm_pipeline_id",
    "keycrm_status_id",
    "calls_enabled",
    "screen_enabled",
    "intake_enabled",
    "open_paid_contacts",
    "vacancy_url",
)

OPS_LABELS = {
    "label": "Назва у CRM",
    "workua_ids": "ID на work.ua",
    "robotaua_ids": "ID на robota.ua",
    "keycrm_pipeline_id": "Воронка CRM",
    "keycrm_status_id": "Етап CRM",
    "calls_enabled": "Єва дзвонить",
    "screen_enabled": "Фільтри (гео, вік)",
    "intake_enabled": "Збирати відгуки",
    "open_paid_contacts": "Платні контакти",
    "vacancy_url": "Посилання",
}

BOOL_FIELDS = ("calls_enabled", "screen_enabled", "intake_enabled", "open_paid_contacts")
INT_FIELDS = ("keycrm_pipeline_id", "keycrm_status_id")
IDSET_FIELDS = ("workua_ids", "robotaua_ids")


class VacancyStoreError(ValueError):
    """Bad input from the panel. Message is shown to the recruiter as-is."""


def _path() -> Path:
    return state_dir() / STORE_NAME


def load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 — a corrupt store must not stop calling
        log.warning("vacancy_store.unreadable", error=str(e))
        return {}


def _write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# --- validation --------------------------------------------------------------

def coerce(field: str, raw: str) -> Any:
    """Turn panel text into a stored value, or explain what is wrong.

    Every operational field goes through here. An unvalidated funnel id is how a
    vacancy silently starts filing candidates into someone else's pipeline.
    """
    raw = (raw or "").strip()
    if field in BOOL_FIELDS:
        low = raw.lower()
        if low in ("так", "yes", "1", "on", "+", "true"):
            return True
        if low in ("ні", "no", "0", "off", "-", "false"):
            return False
        raise VacancyStoreError("Відповідь має бути «так» або «ні».")
    if field in INT_FIELDS:
        if not raw.isdigit():
            raise VacancyStoreError("Потрібне число — id воронки або етапу з KeyCRM.")
        return int(raw)
    if field in IDSET_FIELDS:
        if not raw or raw in ("-", "—", "немає"):
            return []
        parts = re.split(r"[\s,;]+", raw)
        ids = []
        for p in parts:
            if not p:
                continue
            if not p.isdigit():
                raise VacancyStoreError(
                    f"«{p}» — не id. Потрібні лише числа через кому, напр. "
                    "<code>8249916, 8374143</code>."
                )
            ids.append(int(p))
        return sorted(set(ids))
    if field == "label":
        if not raw:
            raise VacancyStoreError("Назва не може бути порожньою.")
        return raw[:100]
    if field == "vacancy_url":
        if raw and not raw.startswith("http"):
            raise VacancyStoreError("Посилання має починатись з http.")
        return raw[:400]
    if field in EDITABLE:
        return raw
    raise VacancyStoreError(f"Поле {field} невідоме.")


def set_field(key: str, field: str, raw: str) -> Any:
    """Record one panel edit. Returns the stored value."""
    if field not in EDITABLE and field not in OPS_FIELDS:
        raise VacancyStoreError(f"поле {field} не редагується з панелі")
    value = coerce(field, raw)
    data = load()
    entry = dict(data.get(key) or {})
    entry[field] = value
    entry["_updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    data[key] = entry
    _write(data)
    log.info("vacancy_store.set", vacancy=key, field=field, value=str(value)[:80])
    return value


def clear_field(key: str, field: str) -> None:
    data = load()
    entry = dict(data.get(key) or {})
    entry.pop(field, None)
    data[key] = entry
    _write(data)
    log.info("vacancy_store.cleared", vacancy=key, field=field)


def create(key: str, label: str) -> None:
    """Register a brand-new vacancy. Starts inert on purpose.

    `intake_enabled` is off and `calls_enabled` is off until the recruiter has
    entered board ids and a funnel — a half-configured vacancy that is already
    collecting would file people into funnel 0.
    """
    key = (key or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{2,32}", key):
        raise VacancyStoreError(
            "Ключ — лише латиниця, цифри й підкреслення, 2–32 символи. Напр. <code>driver</code>."
        )
    data = load()
    if key in data and data[key].get("_custom"):
        raise VacancyStoreError(f"Вакансія «{key}» вже існує.")
    data[key] = {
        "_custom": True,
        "label": coerce("label", label),
        "workua_ids": [],
        "robotaua_ids": [],
        "keycrm_pipeline_id": 0,
        "keycrm_status_id": 0,
        "calls_enabled": False,
        "screen_enabled": False,
        "intake_enabled": False,
        "open_paid_contacts": False,
        "_updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    _write(data)
    log.info("vacancy_store.created", vacancy=key, label=label)


def delete(key: str) -> None:
    """Remove a panel-created vacancy. Shipped ones can only be reset, not deleted."""
    data = load()
    entry = data.get(key)
    if not entry or not entry.get("_custom"):
        raise VacancyStoreError("Вакансію з коду видалити не можна — вимкніть збір і дзвінки.")
    data.pop(key, None)
    _write(data)
    log.info("vacancy_store.deleted", vacancy=key)


def is_custom(key: str) -> bool:
    return bool((load().get(key) or {}).get("_custom"))


def blockers(vac) -> list[str]:
    """Why this vacancy cannot be switched on yet, in words a recruiter can act on."""
    out = []
    if not (vac.workua_ids or vac.robotaua_ids):
        out.append("не вказано жодного ID вакансії на сайтах")
    if not vac.keycrm_pipeline_id:
        out.append("не вказано воронку CRM")
    if not vac.keycrm_status_id:
        out.append("не вказано етап CRM")
    return out


# --- merge -------------------------------------------------------------------

def apply(vacancy):
    """Lay this vacancy's stored edits over the shipped defaults."""
    entry = load().get(vacancy.key) or {}
    patch: dict[str, Any] = {}
    for f, v in entry.items():
        if f in EDITABLE and isinstance(v, str):
            patch[f] = v
        elif f in IDSET_FIELDS and isinstance(v, list):
            patch[f] = frozenset(int(x) for x in v)
        elif f in BOOL_FIELDS and isinstance(v, bool):
            patch[f] = v
        elif f in INT_FIELDS and isinstance(v, int):
            patch[f] = v
        elif f in ("label", "vacancy_url") and isinstance(v, str):
            patch[f] = v
    return dataclasses.replace(vacancy, **patch) if patch else vacancy


def custom_vacancies(factory):
    """Build Vacancy objects for panel-created keys. `factory` is the dataclass."""
    out = {}
    for key, entry in load().items():
        if not entry.get("_custom"):
            continue
        try:
            out[key] = factory(
                key=key,
                label=entry.get("label") or key,
                workua_ids=frozenset(int(x) for x in (entry.get("workua_ids") or [])),
                robotaua_ids=frozenset(int(x) for x in (entry.get("robotaua_ids") or [])),
                keycrm_pipeline_id=int(entry.get("keycrm_pipeline_id") or 0),
                keycrm_status_id=int(entry.get("keycrm_status_id") or 0),
                calls_enabled=bool(entry.get("calls_enabled")),
                screen_enabled=bool(entry.get("screen_enabled")),
                intake_enabled=bool(entry.get("intake_enabled")),
                open_paid_contacts=bool(entry.get("open_paid_contacts")),
                vacancy_url=entry.get("vacancy_url") or "",
                spoken_title=entry.get("spoken_title") or "",
                spoken_salary=entry.get("spoken_salary") or "",
                spoken_schedule=entry.get("spoken_schedule") or "",
                spoken_benefits=entry.get("spoken_benefits") or "",
                spoken_pitch=entry.get("spoken_pitch") or "",
            )
        except Exception as e:  # noqa: BLE001 — one bad entry must not hide the rest
            log.warning("vacancy_store.bad_custom", vacancy=key, error=str(e))
    return out


def spoken(vacancy, field: str) -> str:
    """What Єва should say, or "" to let the global .env default stand."""
    return (getattr(apply(vacancy), field, "") or "").strip()


def describe(vacancy) -> dict[str, dict[str, str]]:
    """Per-field {value, source} so the panel can show whose text this is."""
    entry = load().get(vacancy.key) or {}
    out: dict[str, dict[str, str]] = {}
    for f in EDITABLE:
        if isinstance(entry.get(f), str) and entry[f].strip():
            out[f] = {"value": entry[f], "source": "панель"}
        elif (getattr(vacancy, f, "") or "").strip():
            out[f] = {"value": getattr(vacancy, f), "source": "код"}
        else:
            out[f] = {"value": "", "source": "загальний .env"}
    return out
