# AI Recruiter — Voice HR Pipeline

End-to-end voice AI recruiter that pulls candidate responses from Ukrainian job
boards, qualifies them with an LLM-driven phone interview, and pushes
structured leads into a CRM funnel — with a Telegram bot for daily reports
and ops controls.

Built as a real production system for **Kozyr Trans** (freight logistics,
hiring sales managers / logisticians). Architecture is provider-agnostic and
easy to repurpose for any high-volume hiring funnel.

---

## What it does

```
┌──────────────┐    ┌──────────────┐    ┌─────────────┐
│ work.ua API  │    │  robota.ua   │    │  OLX Jobs   │  ... pluggable
└──────┬───────┘    └──────┬───────┘    └──────┬──────┘
       │                   │                   │
       └─────────┬─────────┴─────────┬─────────┘
                 ▼                   ▼
        ┌──────────────────────────────────┐
        │  InboundRouter (dedup + filter)  │
        │  - phone normalize (E.164)       │
        │  - region whitelist/blacklist    │
        │  - profile filter (age/role/...) │
        └──────────────┬───────────────────┘
                       ▼
              ┌────────────────────┐
              │   KeyCRM lead      │
              │   (funnel = SoT)   │
              └────────┬───────────┘
                       │
                       ▼
        ┌──────────────────────────────────┐
        │  Scheduler (6 cron slots/day)    │
        │  → Vapi outbound call            │
        │     - Anthropic Claude (brain)   │
        │     - Deepgram (STT)             │
        │     - ElevenLabs (TTS)           │
        │     - 11-step interview script   │
        └──────────────┬───────────────────┘
                       ▼
        ┌──────────────────────────────────┐
        │  Post-call: Haiku summary        │
        │  → KeyCRM stage move             │
        │  → Telegram report at 09:00      │
        └──────────────────────────────────┘
```

Bi-directional: same Telnyx/Twilio number takes inbound dials, routes to
the same assistant. Candidates can call you back; the assistant qualifies them
on the spot and creates the lead.

---

## Stack

| Layer | Tool | Why |
|---|---|---|
| Language model | Claude Haiku 4.5 / Sonnet 4.x | Fast Ukrainian, strong instruction-following, function-calling for the state machine |
| Speech-to-text | Deepgram Nova-2 (uk) | Ukrainian-tuned, low latency |
| Text-to-speech | ElevenLabs Flash v2.5 | Natural Ukrainian voice, sub-second streaming |
| Voice orchestration | Vapi.ai | WebSocket bridge, VAD, barge-in, SIP integrations |
| Telephony | Telnyx / Twilio BYOC | UA/international outbound (Vapi-native numbers do not support intl) |
| CRM | KeyCRM | Lead funnel, single source of truth |
| Job boards | work.ua (live) + robota.ua / OLX / Jooble (stubs) | Pluggable `JobBoardProvider` |
| API | FastAPI + uvicorn | Webhooks: Vapi, KeyCRM, work.ua |
| Scheduler | APScheduler | 6 daily call slots + 5-min poll loops |
| Bot | python-telegram-bot | Daily report + admin commands |
| DB | PostgreSQL 16 | Persistent state, call logs, cost tracking |
| Migrations | Alembic | Schema versioning |
| Reverse proxy | Caddy | Auto-HTTPS via Let's Encrypt |
| Deploy | Docker Compose on Hetzner CPX22 (Helsinki) | Low UA latency, EU jurisdiction |

---

## Features

- **Pluggable provider abstraction** (`src/integrations/base.py`) — add any
  job board in two files (`<board>_api.py` + `<board>_sync.py`); env-flag
  gates pick it up at scheduler startup.
- **InboundRouter** with phone E.164 normalization, region whitelist/blacklist,
  multi-step profile filter (age window, recent-role check, war-pause exception).
- **11-step interview script** rendered from `src/call/script_template.py` —
  greeting → work.ua intent → region → pitch + experience → 3 behavioral
  questions → motivation → salary → schedule/remote → tech readiness →
  interest confirmation → handoff to recruiter.
- **Bi-directional voice** — same Vapi assistant answers inbound dials AND
  drives outbound calls; inbound branch auto-creates a Candidate + KeyCRM
  lead from caller phone.
- **Post-call summarizer** — Claude Haiku turns transcript into 3 bullets +
  sentiment + qualification flag → KeyCRM custom fields updated automatically.
- **Telegram admin bot** — `/status`, `/pause_workua`, `/resume_workua`,
  `/set_threshold`, `/test_call`, `/queue`, `/params`. Daily report at 09:00
  Europe/Kyiv.
- **Cost tracking** — per-call token + minute breakdown stored in `daily_costs`;
  rolled up in the daily report.
- **Idempotent KeyCRM bootstrap** — `scripts/keycrm_bootstrap.py` provisions
  the funnel + custom fields once, survives re-runs.
- **Guardrails** — profanity detector, repetition counter, soft-exit reasons
  enumerated; 7-min hard cap; topic redirector for politics/religion/war.
- **Webhook auth** — dual-mode for Vapi: `X-Vapi-Signature` HMAC OR
  `X-Vapi-Secret` raw shared secret (Vapi sends the latter by default).
- **Native Postgres ENUM bypass** — status columns use `VARCHAR(32)` storing
  Python enum names; avoids the SQLAlchemy ↔ Postgres enum sync trap.

---

## Architecture decisions

- **work.ua first, robota.ua second** — work.ua exposes a free official API
  for inbound responses; robota.ua requires a partner agreement. The
  pluggable registry keeps the bot working while approvals land in parallel.
- **BYOC telephony** — Vapi-provided free US numbers cannot make international
  calls. Telnyx Call Control App + a verified account unlocks UA destinations
  with a US `+1 (415)` number that UA carriers actually route.
- **VARCHAR over native enum** — Postgres native ENUM types collide with
  SQLAlchemy's mapped enum if migrations are written manually. Storing the
  enum *name* in `VARCHAR(32)` keeps the Python side ergonomic and the SQL
  side trivial to migrate.
- **Inbound + outbound share the assistant** — same Vapi assistant ID, same
  system prompt; for inbound, placeholder fields are populated with
  "невідомо (запитати у кандидата)" so the script naturally asks instead of
  reading a `{REGION}` literal.

---

## Repository layout

```
ai-recruiter/
├── deploy/                 # docker-compose, Dockerfile, Caddyfile
├── docs/                   # specs, KeyCRM schema, call script, candidate profile
├── migrations/             # Alembic migrations (incl. enum→varchar fix)
├── scripts/                # bootstrap helpers, Vapi assistant patcher
├── src/
│   ├── api/                # FastAPI app, schemas, services, inbound router
│   ├── bot/                # Telegram polling bot — admin + daily report
│   ├── call/               # Vapi orchestrator, script template, summarizer
│   ├── common/             # settings, DB, models, KeyCRM client, phone util
│   ├── guardrails/         # profanity / repetition / topic redirect
│   ├── integrations/       # job board providers + registry
│   │   ├── base.py         # JobBoardProvider abstract contract
│   │   ├── workua_*.py     # work.ua — live
│   │   ├── robotaua_*.py   # robota.ua — stub
│   │   ├── jooble_api.py   # meta-search — stub
│   │   └── olx_jobs_*.py   # OLX Jobs UA — stub
│   ├── match/              # candidate ↔ vacancy scoring + profile filter
│   ├── scheduler/          # APScheduler dispatcher (6 slots + polls)
│   └── scraper/            # work.ua Playwright fallback (rate-limited)
└── tests/                  # pytest — guardrails, fsm, phone, regions, ...
```

---

## Quick start (local dev)

```bash
# 1. Clone + venv
git clone https://github.com/<you>/ai-recruiter.git
cd ai-recruiter
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Copy env template, fill in keys
cp .env.example .env
$EDITOR .env

# 3. Boot Postgres + run migrations
docker compose -f deploy/docker-compose.yml up -d db
alembic upgrade head

# 4. Run API + scheduler + bot
docker compose -f deploy/docker-compose.yml up -d
curl http://localhost:8000/health    # {"status":"ok","env":"dev"}
```

Required keys to actually make calls (everything else has sensible defaults):

- `ANTHROPIC_API_KEY`
- `DEEPGRAM_API_KEY` + `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`
- `VAPI_API_KEY` + `VAPI_ASSISTANT_ID` + `VAPI_WEBHOOK_SECRET` + `VAPI_PHONE_NUMBER_ID`
- `KEYCRM_API_TOKEN` + `KEYCRM_FUNNEL_ID`
- `TG_REPORT_BOT_TOKEN` + `TG_REPORT_CHAT_ID`
- One of: `TELNYX_API_KEY` **or** `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`

Optional providers — drop in the key, restart, the scheduler picks them up:

- `ROBOTAUA_API_TOKEN`
- `OLX_JOBS_CLIENT_ID` + `OLX_JOBS_CLIENT_SECRET`
- `JOOBLE_API_KEY` (sourcing-only)

---

## Production deploy

```bash
# On a fresh Hetzner CPX22 (or any 2-vCPU / 4 GB / Docker-capable host):
ssh root@<vps>
bash <(curl -fsSL https://raw.githubusercontent.com/<you>/ai-recruiter/main/scripts/vps_bootstrap.sh)

# Then:
cd /opt/ai-recruiter
cp .env.example .env && $EDITOR .env
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml exec api alembic upgrade head
```

Caddy auto-provisions a TLS cert for the configured domain and proxies the
FastAPI app at `/health`, `/webhooks/vapi/events`, `/webhooks/keycrm/{event}`,
`/webhooks/workua/manual`.

---

## Adding a new job board

```python
# src/integrations/foo_api.py
class FooClient:
    async def list_new_applications(self, since: str | None) -> list[dict]: ...

# src/integrations/foo_sync.py
from src.integrations.base import JobBoardProvider, PollResult

class FooProvider(JobBoardProvider):
    name = "foo"
    required_env = ("FOO_API_KEY",)

    async def poll_responses(self) -> PollResult: ...
    async def health_check(self) -> bool: ...
```

Then register it in `src/integrations/registry.py::PROVIDERS`. Done — the
scheduler reads `enabled_providers()` at startup and skips any provider whose
required env vars are blank.

---

## Telegram admin

| Command | Effect |
|---|---|
| `/status` | Scheduler state, queue size, today's stats |
| `/queue` | Next 10 candidates due to be called |
| `/pause_workua` | Stop the work.ua poller |
| `/resume_workua` | Resume |
| `/set_threshold 0.7` | Change `MATCH_SCORE_THRESHOLD` at runtime |
| `/test_call +380...` | Trigger an immediate outbound test call |
| `/params` | Dump current runtime config |
| `/help` | List commands |

Daily report fires at `TG_REPORT_HOUR`:`TG_REPORT_MINUTE` (default 09:00 EET).

---

## Known limitations / current status

- **Outbound through Vapi → Telnyx BYOC has a one-way-audio bug** on the Vapi
  side; ticket open with their support. Inbound works fully both ways.
- **Twilio Trial accounts** get flagged by their compliance bot when a new
  account dials UA immediately — switch to a fully verified Twilio account or
  use Telnyx.
- **robota.ua / OLX Jobs partner APIs** are gated behind written agreements;
  the integration code is scaffolded with `NotImplementedError` until tokens
  arrive.

---

## License

[Proprietary — All Rights Reserved](./LICENSE). Source published for portfolio
and demonstration purposes only. No personal or commercial use without prior
written permission.

---

## Author

Built by Bohdan Havryliuk. Production system for
[Kozyr Trans](https://kozyrtrans-ai.com), 2026.
