"""Bootstrap KeyCRM: create funnel + 9 stages + 24 custom fields, idempotent.

Usage:
    python3 scripts/keycrm_bootstrap.py

Reads creds from .env. Idempotent — re-running won't duplicate fields/stages,
will just print what was already there. After success, writes funnel_id + a
mapping JSON next to it for use by the app.
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("ERROR: .env not found", file=sys.stderr)
        sys.exit(1)
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        # strip trailing inline comments (everything after first space then #)
        if "#" in v:
            v = v.split("#", 1)[0]
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
TOKEN = ENV.get("KEYCRM_API_TOKEN", "")
BASE = ENV.get("KEYCRM_BASE_URL", "https://openapi.keycrm.app/v1").rstrip("/")
CTX = ssl.create_default_context(cafile=certifi.where())


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=20, context=CTX)
        text = r.read().decode()
        try:
            return r.getcode(), json.loads(text)
        except ValueError:
            return r.getcode(), text
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:500]
        try:
            return e.code, json.loads(body_text)
        except ValueError:
            return e.code, body_text


FUNNEL_NAME = "AI Recruiter — Kozyr Trans"

STAGES = [
    ("new_resume", "🆕 Нові резюме", "#9CA3AF"),
    ("filtered", "🎯 Пройшли фільтр", "#60A5FA"),
    ("in_call_queue", "📞 В черзі дзвінків", "#34D399"),
    ("calling", "🔁 Дозвонюємось", "#FBBF24"),
    ("unreachable", "⛔ Не дозвонились", "#9CA3AF"),
    ("call_done", "💬 Дзвінок відбувся", "#A78BFA"),
    ("manager_review", "⭐ У менеджера", "#F472B6"),
    ("interview_scheduled", "✅ Інтервʼю заплановане", "#10B981"),
    ("closed", "🏁 Закрито", "#6B7280"),
]

# tech_key -> (Display name, type)
# KeyCRM Custom Field types: text, number, datetime, url, select, multiselect, checkbox, textarea
CUSTOM_FIELDS = [
    ("work_ua_url", "URL резюме work.ua", "url"),
    ("region", "Регіон", "text"),
    ("desired_position", "Бажана посада", "text"),
    ("experience_years", "Досвід (років)", "number"),
    ("languages", "Мови (uk/ru/en/pl/de)", "text"),
    ("match_score", "Match score (0-100)", "number"),
    ("vacancy_id", "Vacancy ID (work.ua)", "number"),
    ("call_attempts", "Спроб дзвінка", "number"),
    ("last_call_at", "Останній дзвінок", "datetime"),
    ("last_call_status", "Статус останнього дзвінка", "text"),
    ("last_call_duration_sec", "Тривалість дзвінка (сек)", "number"),
    ("audio_url", "Аудіо запис", "url"),
    ("transcript", "Транскрипт", "textarea"),
    ("ai_summary", "AI-резюме (3 буліти)", "textarea"),
    ("sentiment", "Sentiment", "text"),
    ("objections_raised", "Заперечення", "text"),
    ("language_used", "Мова розмови", "text"),
    ("tokens_input", "Tokens in", "number"),
    ("tokens_output", "Tokens out", "number"),
    ("cost_usd", "Вартість дзвінка USD", "number"),
    ("tags", "Теги", "text"),
    ("manager_assigned", "Менеджер", "text"),
    ("interview_scheduled_at", "Дата інтервʼю", "datetime"),
    ("source", "Source (workua_response/workua_search/manual)", "text"),
]


def ensure_funnel() -> int:
    code, data = api("GET", "/pipelines")
    if code != 200:
        print(f"❌ GET /pipelines → {code}: {data}")
        sys.exit(1)
    items = data.get("data") if isinstance(data, dict) else data or []
    for f in items:
        if f.get("name") == FUNNEL_NAME:
            print(f"✅ funnel exists: id={f.get('id')} '{FUNNEL_NAME}'")
            return int(f["id"])
    code, created = api("POST", "/pipelines", {"name": FUNNEL_NAME})
    if code not in (200, 201):
        print(f"❌ POST /pipelines → {code}: {created}")
        sys.exit(1)
    fid = int(created.get("id") or created.get("data", {}).get("id"))
    print(f"✅ funnel created: id={fid} '{FUNNEL_NAME}'")
    return fid


def ensure_stages(funnel_id: int) -> dict[str, int]:
    code, data = api("GET", f"/pipelines/{funnel_id}/stages")
    if code != 200:
        print(f"❌ GET stages → {code}: {data}")
        return {}
    existing = {s.get("name"): int(s["id"]) for s in (data.get("data") if isinstance(data, dict) else data) or []}
    mapping: dict[str, int] = {}
    for order, (tech_key, name, color) in enumerate(STAGES, start=1):
        if name in existing:
            mapping[tech_key] = existing[name]
            print(f"✅ stage exists: {name} → id={existing[name]}")
            continue
        body = {"name": name, "color": color, "sort_order": order}
        code, created = api("POST", f"/pipelines/{funnel_id}/stages", body)
        if code not in (200, 201):
            print(f"❌ POST stage '{name}' → {code}: {created}")
            continue
        sid = int(created.get("id") or created.get("data", {}).get("id"))
        mapping[tech_key] = sid
        print(f"✅ stage created: {name} → id={sid}")
        time.sleep(0.3)
    return mapping


def ensure_custom_fields() -> dict[str, int]:
    # Custom fields in KeyCRM are global (per entity type)
    # Endpoint: /custom-fields?entity=lead (or similar — adjust if needed after first run)
    code, data = api("GET", "/custom-fields?entity_type=lead")
    if code != 200:
        # Try alternative endpoint
        code, data = api("GET", "/custom-fields")
    if code != 200:
        print(f"⚠️  could not fetch custom fields (HTTP {code}). Will try to create anyway.")
        existing_by_name = {}
    else:
        items = (data.get("data") if isinstance(data, dict) else data) or []
        existing_by_name = {f.get("name"): int(f["id"]) for f in items}

    mapping: dict[str, int] = {}
    for tech_key, name, ftype in CUSTOM_FIELDS:
        if name in existing_by_name:
            mapping[tech_key] = existing_by_name[name]
            print(f"✅ field exists: {name} → id={existing_by_name[name]}")
            continue
        body = {
            "name": name,
            "code": tech_key,
            "type": ftype,
            "entity_type": "lead",
        }
        code, created = api("POST", "/custom-fields", body)
        if code not in (200, 201):
            print(f"❌ POST field '{name}' → {code}: {created}")
            continue
        fid = int(created.get("id") or created.get("data", {}).get("id") or 0)
        if fid:
            mapping[tech_key] = fid
            print(f"✅ field created: {name} → id={fid}")
        time.sleep(0.3)
    return mapping


def write_mapping(funnel_id: int, stage_map: dict, field_map: dict) -> None:
    out = ROOT / "keycrm_mapping.json"
    out.write_text(
        json.dumps(
            {
                "funnel_id": funnel_id,
                "funnel_name": FUNNEL_NAME,
                "stages": stage_map,
                "fields": field_map,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n📝 mapping saved → {out.relative_to(ROOT)}")

    # Also patch .env with funnel_id
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    patched = []
    found = False
    for line in lines:
        if line.startswith("KEYCRM_FUNNEL_ID="):
            patched.append(f"KEYCRM_FUNNEL_ID={funnel_id}")
            found = True
        else:
            patched.append(line)
    if not found:
        patched.append(f"KEYCRM_FUNNEL_ID={funnel_id}")
    env_path.write_text("\n".join(patched) + "\n", encoding="utf-8")
    print(f"📝 .env patched: KEYCRM_FUNNEL_ID={funnel_id}")


def main() -> None:
    if not TOKEN:
        print("ERROR: KEYCRM_API_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    print(f"Using KeyCRM at {BASE}")
    print(f"Token prefix: {TOKEN[:8]}...")

    funnel_id = ensure_funnel()
    print()
    stage_map = ensure_stages(funnel_id)
    print()
    field_map = ensure_custom_fields()
    print()
    write_mapping(funnel_id, stage_map, field_map)
    print()
    print(f"DONE. {len(stage_map)} stages, {len(field_map)} fields wired up.")


if __name__ == "__main__":
    main()
