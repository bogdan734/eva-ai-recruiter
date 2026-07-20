# AI Recruiter — Phase 1

Voice AI HR pipeline: work.ua → KeyCRM → Ringostat/Vapi voice agent → daily Telegram report.

## Stack
- **Python 3.12** + FastAPI (REST API + webhooks)
- **Playwright** (work.ua scraping)
- **Apix-Drive** (work.ua inbound responses → KeyCRM)
- **Claude Sonnet 4.6** (dialogue brain + embedding match + post-call summary)
- **Deepgram Nova-3** (STT streaming, uk/ru/en)
- **ElevenLabs Flash v2.5** (TTS, uk voice)
- **Vapi.ai** (voice orchestration, WebSocket bridge, VAD, barge-in)
- **Twilio / Ringostat** (telephony, +380 number)
- **KeyCRM** (lead funnel, source of truth)
- **python-telegram-bot** (separate daily report bot, 9:00 EET)
- **NocoDB / SQLite** (call logs, cost tracking)
- **Hetzner CX22 Helsinki** (VPS, low RTT to UA)
- **Caddy** (reverse proxy, auto-HTTPS)
- **sops + age** (encrypted secrets)

## Project layout
```
ai-recruiter/
├── docs/                    # Specs, schemas, research notes
├── src/
│   ├── api/                 # FastAPI app, webhooks, REST
│   ├── scheduler/           # Cron-based call dispatcher
│   ├── scraper/             # work.ua Playwright scraper
│   ├── bot/                 # Telegram daily report bot
│   ├── call/                # Vapi orchestration glue
│   ├── match/               # Embedding match (candidate ↔ vacancy)
│   ├── guardrails/          # Profanity, repetition, exit detection
│   ├── cost/                # Token/usage cost tracking
│   └── common/              # Shared models, db, settings
├── tests/
├── deploy/                  # docker-compose, Caddyfile, systemd
└── scripts/                 # one-off ops scripts
```

## Quick start (dev)
```bash
cp .env.example .env       # fill in your keys
docker compose up -d
curl http://localhost:8000/health
```

## Deploy
See `deploy/README.md` (Hetzner CX22 + Caddy + docker-compose).

## Status
- [x] Repo skeleton
- [x] KeyCRM schema spec
- [x] Apix-Drive setup guide
- [x] Ringostat architecture research
- [x] Vapi config template
- [x] Scraper prototype (public pages)
- [x] Embedding match module
- [x] Guardrails module
- [x] Cost tracker
- [x] TG bot skeleton
- [x] Daily report aggregator
- [ ] Call script v1 (waiting from client)
- [ ] Infrastructure provisioned (waiting for credentials)
- [ ] KeyCRM funnel + fields (waiting for API token)
- [ ] Apix-Drive live (waiting for work.ua employer account)
- [ ] Vapi assistant live (waiting for Twilio/Ringostat decision)
- [ ] E2E test (waiting on above)
