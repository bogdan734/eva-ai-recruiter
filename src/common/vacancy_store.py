"""Edits to the vacancy registry that survive a deploy.

`vacancies.py` holds the shipped defaults. Anything a recruiter changes from the
Telegram panel lands here instead — a small JSON file on the mounted state
volume, merged over those defaults on read.

Why not just edit `.env` like the old vacancy fields did: `.env` has one value
per setting for the whole system, which is exactly why every candidate heard the
same pitch no matter which posting they answered. And why not write back into
`vacancies.py`: code is baked into the image at build time, so an edit made from
the panel would vanish on the next `docker compose build` — the same trap that
ate the work.ua cursor for weeks.

Only the spoken script fields are editable. Ids, funnels and the calls/screening
flags stay in code on purpose: getting those wrong routes candidates into the
wrong funnel or starts calling people nobody meant to call, and that is a change
worth a deploy and a code review.
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from src.common.state import state_dir

log = structlog.get_logger()

STORE_NAME = "vacancy_overrides.json"

# Editable from the panel. Everything else needs a deploy — see module docstring.
EDITABLE = ("spoken_title", "spoken_salary", "spoken_schedule", "spoken_benefits", "spoken_pitch")

# Human labels for the panel, so the button text lives next to the field list.
LABELS = {
    "spoken_title": "Посада",
    "spoken_salary": "Зарплата",
    "spoken_schedule": "Графік",
    "spoken_benefits": "Умови",
    "spoken_pitch": "Про компанію",
}


def _path() -> Path:
    return state_dir() / STORE_NAME


def load() -> dict[str, dict[str, Any]]:
    """{vacancy_key: {field: value}}. Missing or unreadable file means no edits."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 — a corrupt file must not stop calling
        log.warning("vacancy_store.unreadable", error=str(e))
        return {}


def _write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic: a half-written file would read as "no edits"


def set_field(key: str, field: str, value: str) -> None:
    """Record one panel edit. Unknown fields are refused, not silently stored."""
    if field not in EDITABLE:
        raise ValueError(f"поле {field} не редагується з панелі")
    data = load()
    entry = dict(data.get(key) or {})
    entry[field] = value
    entry["_updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    data[key] = entry
    _write(data)
    log.info("vacancy_store.set", vacancy=key, field=field, chars=len(value))


def clear_field(key: str, field: str) -> None:
    """Drop an override so the vacancy falls back to the shipped default."""
    data = load()
    entry = dict(data.get(key) or {})
    entry.pop(field, None)
    data[key] = entry
    _write(data)
    log.info("vacancy_store.cleared", vacancy=key, field=field)


def apply(vacancy):
    """Return the vacancy with its stored edits laid over the shipped defaults."""
    entry = load().get(vacancy.key) or {}
    patch = {f: v for f, v in entry.items() if f in EDITABLE and isinstance(v, str)}
    return dataclasses.replace(vacancy, **patch) if patch else vacancy


def spoken(vacancy, field: str) -> str:
    """The value Єва should say, or "" to let the global .env default stand.

    Kept here rather than in the caller so the fallback order is written down
    once: panel edit -> shipped default -> global .env.
    """
    return (getattr(apply(vacancy), field, "") or "").strip()


def describe(vacancy) -> dict[str, dict[str, str]]:
    """Per-field {value, source} for the panel, so a recruiter can see at a glance
    which text is theirs and which is still the shipped or global one."""
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
