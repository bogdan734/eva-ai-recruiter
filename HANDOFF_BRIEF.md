# AI Recruiter — Handoff Brief (для нового чату)

**Дата**: 2026-06-25
**Клієнт**: Kozyr Trans (вантажоперевезення)
**Вакансія**: "Менеджер з продажу, логіст" — повністю віддалена, 5-денка 9-17, ЗП 30-65к грн+

---

## ЩО ЦЕ

Voice AI recruiter для Kozyr Trans. Тягне відгуки з **work.ua → KeyCRM**, у слотах **9/11/13/15/17/19** обзвонює кандидатів через **Vapi+Claude+Deepgram+ElevenLabs** з персоною **Єва**. Кожен ранок о **9:00** — звіт у Telegram.

Production стенд live: **https://api.kozyrtrans-ai.com/health** → `{"status":"ok"}`

---

## АРХІТЕКТУРА

```
work.ua API ─┐
             ├─→ scheduler/dispatcher ─→ KeyCRM lead ─→ Vapi outbound call (Єва)
KeyCRM webhook (зворотно)              ↓
                                 post-call: Claude summary → KeyCRM update
                                          ↓
                                 TG bot (звіт 9:00 + admin commands)
```

**Сервіси (Docker на Hetzner CPX22 Helsinki, 65.21.151.71):**
- `api` (FastAPI :8000) — webhooks Vapi/KeyCRM/work.ua
- `scheduler` — APScheduler: 6 cron slots + workua poll кожні 5 хв
- `bot` — Telegram polling: report + admin commands
- `caddy` — reverse proxy + Let's Encrypt SSL
- `db` — PostgreSQL 16

---

## ВЖЕ ЗРОБЛЕНО

| Категорія | Деталі |
|---|---|
| **Інфра** | Hetzner CPX22 (`65.21.151.71`), Cloudflare DNS (`kozyrtrans-ai.com`), Let's Encrypt SSL |
| **AI стек** | Anthropic Claude 4.6, Deepgram Nova-3, ElevenLabs Creator (голос Nastasia), Vapi Assistant Єва опублікована |
| **Телефонія** | Vapi US +1(831)303-0188 (для тесту). Twilio +380 KYC поданий — чекаємо 3-7 днів |
| **CRM** | KeyCRM воронка `pipeline_id=1` "1 Етап Менеджер з продажу" + 9 кастомних полів (LD_1001..LD_1009) |
| **work.ua** | API підключений, 56 вакансій в акаунті, 10+ відгуків регулярно прилітає |
| **TG бот** | `@KozyrTransHRBot` (token у `.env`), chat_id `8727814087`, daily report + admin commands |
| **Код** | ~80 файлів Python, 12 тестових файлів, Alembic міграції, CI на GitHub Actions |

---

## ЩО ЧЕКАЄ

1. **Twilio +380 KYC** (3-7 днів) — підключаємо до Vapi → перші реальні UA дзвінки
2. (опц.) Бекап-канал robota.ua API
3. (опц.) `.ai` домен якщо клієнт захоче брендового

---

## КЛЮЧОВІ ФАЙЛИ

### Локально (Mac)
```
/Users/jabko/ai-recruiter/
├── .env                          # ✅ 18 ключів заповнено (НЕ комітити!)
├── src/
│   ├── api/main.py               # FastAPI: /health /webhooks/keycrm /webhooks/vapi /webhooks/workua
│   ├── api/inbound_router.py     # work.ua/Apix → dedup → KeyCRM lead create
│   ├── call/script_template.py   # System prompt Єви, 11 кроків
│   ├── call/orchestrator.py      # Vapi dispatch + post-call summary
│   ├── call/summarizer.py        # Claude Haiku: transcript → 3 буліти + sentiment
│   ├── common/keycrm.py          # KeyCRM Open API client + FIELD/STAGE constants
│   ├── common/keycrm_fields.py   # LD_1001..LD_1009 mapping
│   ├── integrations/workua_api.py # work.ua REST (HTTP Basic, з email/password)
│   ├── integrations/workua_sync.py # poll_responses() — cron кожні 5 хв
│   ├── match/profile_filter.py   # 5 правил: вік 23-42 (Ж)/23-40 (Ч), мін. 22 з освітою, 3-річна планка ролі, регіон blacklist, war-pause exception
│   ├── match/scorer.py           # Claude scoring vacancy ↔ candidate
│   ├── scheduler/dispatcher.py   # 6 cron slots + workua_poll кожні 5 хв
│   ├── bot/main.py               # Telegram polling
│   ├── bot/admin.py              # 🎛 /status /pause /resume /queue /test_call /params /set_threshold
│   ├── bot/report.py             # Daily report markdown
│   └── guardrails/               # profanity, repetition, soft-exit
├── deploy/
│   ├── docker-compose.yml        # 5 services
│   ├── Dockerfile
│   └── Caddyfile                 # api+webhooks subdomains
├── scripts/
│   ├── vps_bootstrap.sh          # Hetzner init (Docker, ufw, fail2ban, sops)
│   ├── cloudflare_dns_setup.py   # DNS A records → IP
│   └── keycrm_bootstrap.py       # idempotent KeyCRM setup
├── docs/
│   ├── candidate_profile.md      # портрет кандидата + Q&A від клієнта
│   ├── call_script_v1_kozyr_trans.md # повний 11-step скрипт Єви
│   ├── keycrm_schema.md          # воронка + поля
│   ├── workua_api_integration.md # work.ua API доку шорт
│   └── consent_legal.md          # ЗУ "Про захист персданих" + GDPR фрази
└── keycrm_mapping.json           # funnel_id + stage_ids + field_ids snapshot
```

### На VPS (Hetzner `root@65.21.151.71`)
```
/opt/ai-recruiter/                # same layout, `.env` chmod 600
```

---

## КЛЮЧІ В .ENV (всі live)

```
ANTHROPIC_API_KEY=<REDACTED>
DEEPGRAM_API_KEY=<REDACTED>
ELEVENLABS_API_KEY=<REDACTED>
ELEVENLABS_VOICE_ID=UrqGMRmIdd73BHEhBvt6              # Nastasia UA female
VAPI_API_KEY=<REDACTED>
VAPI_ASSISTANT_ID=88ecfb7b-b732-...                   # Єва опублікована
VAPI_PHONE_NUMBER_ID=6a4aba21-...                     # +1(831)303-0188 test
KEYCRM_API_TOKEN=<REDACTED>
KEYCRM_FUNNEL_ID=1
WORKUA_EMPLOYER_EMAIL=kozyrtranshrmanager@gmail.com   # HTTP Basic
WORKUA_EMPLOYER_PASSWORD=...
CLOUDFLARE_API_TOKEN=<REDACTED>
CLOUDFLARE_ZONE_ID=57c51bea8c864d7c18f6a1cc1b792130
TG_REPORT_BOT_TOKEN=<REDACTED>
TG_REPORT_CHAT_ID=8727814087                          # user M
TG_ADMIN_CHAT_IDS=8727814087                          # same; додавати запис через кому
HETZNER_VPS_IP=65.21.151.71
```

---

## КОМАНДИ TG БОТА

```
/status        — стан + воронка
/queue         — кандидати по статусах
/params        — налаштування з .env
/report        — звіт зараз
/pause         — зупинити дзвонилку
/resume        — продовжити
/pause_workua  — зупинити пуллер work.ua
/resume_workua — продовжити
/set_threshold 0.7   — поріг match-score
/test_call +380...   — тестовий дзвінок Єви
/help          — повний список
```

State persistується в `/tmp/ai_recruiter_state.json` всередині контейнера. На перезапуск перечитується.

---

## ПОРТРЕТ КАНДИДАТА (5 правил клієнта)

| # | Правило | Дія |
|---|---|---|
| 1 | Вік 22 з профільною освітою | Ж: 22-42 / Ч: 22-40 з логістика/продажі/менеджмент/маркетинг/економіка диплом |
| 2 | Профільна освіта | Логістика **та/або** продажі (вкл. суміжні) |
| 3 | Минулі самозайняті + зараз продажі | OK — пропускає |
| 4 | За 3 роки роль "менеджер" обов'язково | Інакше reject. Виняток: гап закінч. ~2022 (війна) |
| 5 | Невідомий тип продажу в резюме | Пропускає — AI уточнює на дзвінку |

**Регіони whitelist**: Київська (без м. Київ), Житомирська, Вінницька, Хмельницька, Тернопільська, Львівська, Івано-Франківська, Закарпатська, Чернівецька, Рівненська, Волинська, Черкаська.

**Регіони blacklist**: м. Київ, Суми, Запоріжжя, Херсон, Донецька, не-Україна.

---

## ЯК ЗАПУСТИТИ З НУЛЯ (disaster recovery)

```bash
# 1. На Mac — клон проекту з ~/ai-recruiter
# 2. SSH у VPS
ssh -i ~/.ssh/id_ed25519 root@65.21.151.71

# 3. Перевірка статусу
cd /opt/ai-recruiter
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=50

# 4. Якщо все лежить — підняти
docker compose -f deploy/docker-compose.yml up -d

# 5. Якщо треба зробити update коду з Mac:
rsync -avz -e "ssh -i ~/.ssh/id_ed25519" \
  --exclude='.git' --exclude='__pycache__' \
  src/ deploy/ root@65.21.151.71:/opt/ai-recruiter/
docker compose -f deploy/docker-compose.yml build api scheduler bot
docker compose -f deploy/docker-compose.yml up -d
```

---

## ЯК ПРОДОВЖИТИ В НОВОМУ ЧАТІ — стартовий промпт

```
Я продовжую роботу над AI Recruiter для Kozyr Trans.

Контекст: див. /Users/jabko/ai-recruiter/HANDOFF_BRIEF.md — там повний стан системи.

Зараз поточний пріоритет: [впиши свій тут]

Можливі напрямки які лишилось зробити:
1. Twilio +380 KYC підтверджено? Підключити до Vapi.
2. Створити TG канал (broadcast) для огляду — окремо від керуючого бота.
3. Додати другу вакансію в систему.
4. Покращити post-call analytics в KeyCRM.
5. Підключити robota.ua API як backup до work.ua.
6. Перевірити alerting (Sentry/Uptime Kuma).
7. Backup БД на Hetzner Storage Box.
```

---

## ЦІНИ /міс (стандартна робота, ~200 дзвінків)

| Сервіс | $/міс |
|---|---|
| Hetzner CPX22 + IPv4 | 23.59 |
| Cloudflare домен (амортизація) | 0.87 |
| Anthropic Claude (200 дзв × ~$0.08) | 16 |
| Deepgram (free до $200 кредит вичерпається) | 0-3 |
| ElevenLabs Creator | 22 |
| Vapi orchestration | 30 |
| Twilio +380 + хвилини | 30 |
| **Разом** | **~$125** |

Plus once-off: $93 starter депозити (вже сплачено).

---

## КОНТАКТИ

- **Hetzner**: `hragent908@gmail.com`, client K0630614626
- **Cloudflare/Vapi/Deepgram/ElevenLabs/Anthropic**: all on `hragent908@gmail.com`
- **work.ua employer**: `kozyrtranshrmanager@gmail.com`
- **KeyCRM admin**: Svitlana Kozyrtrans (`kozyrtranshrmanager@gmail.com`)
- **TG admin chat**: 8727814087 (юзер M, `@DCm1ster`)
- **Telegram bot**: `@KozyrTransHRBot`

---

## ВАЖЛИВО

- **work.ua пароль** є в `.env` — він був скинутий у чаті, **перевипусти** при першій зручній нагоді
- **KeyCRM токен** так само був скинутий — перевипусти
- **TG admin** — додай ще chat_id директора через кому в `TG_ADMIN_CHAT_IDS`
- **Test US номер** Vapi — на проді треба буде +380 від Twilio
- **Голос Nastasia** клієнт підтвердив, не міняти без узгодження
- **Скрипт Єви** — `docs/call_script_v1_kozyr_trans.md` версія v1 від клієнта

---

**END OF BRIEF**
