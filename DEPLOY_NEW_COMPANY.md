# Розгортання AI-рекрутера для нової компанії (playbook)

Цей код — **рушій**, який не прив'язаний до конкретної компанії. Уся специфіка
клієнта живе в `.env` + кількох текстових файлах. Щоб продати/запустити для нової
компанії — не міняй логіку, а зроби нову конфігурацію.

## 1. Архітектура (що з чого)

Docker-compose (`deploy/docker-compose.yml`), 6 сервісів:
- **api** — FastAPI: вебхуки Vapi (кінець дзвінка + `assistant-request` для inbound-роутингу) → summarizer → CRM; вебхук KeyCRM (`/webhooks/keycrm/{event}` — синк ручних змін стадії); ендпоінти `/health`, `/recordings/{id}`, `/resume/{id}`, `/internal/tg-outcome`, `/internal/tg-gate` (стоп Єви після handoff), `/internal/tg-progress` (live-транскрипт у картку).
- **scheduler** — APScheduler: слоти дзвінків (CALL_SLOTS), поллер work.ua/robota.ua кожні 5хв, reconcile кожні 10хв, **sync_crm_stages** кожні 10хв (дзеркалить ручні зміни стадії з CRM → зупиняє Єву), звіт.
- **bot** — Telegram адмін-панель (`/menu`): тест-дзвінки, критерії, пауза, звіт.
- **tguserbot** — Telegram-юзербот (реальний акаунт, MTProto): переписка з кандидатами, персона, дедуп peer↔phone.
- **db** — Postgres.
- **caddy** — reverse-proxy + TLS. ВАЖЛИВО: `reverse_proxy` з `dynamic a` (re-resolve IP кожні 10с), інакше після rebuild api вебхуки мертві.

Зовнішні сервіси (акаунт на КОЖНУ компанію окремо):
- **Vapi** — голосовий оркестратор (assistant = Claude Haiku + Deepgram + ElevenLabs).
- **Телефонія** — SIP-транк (ми: Stream Telecom; альтернативи Telnyx/Zadarma). Дає номер + канали.
- **CRM** — KeyCRM (Open API). Можна замінити на Bitrix/інше — переписати `src/common/keycrm.py`.
- **Anthropic** (Claude), **Deepgram** (STT), **ElevenLabs** (TTS) — ключі.
- **Джерела кандидатів** — work.ua API (офіційний) + robota.ua (скрапер).

## 1.5. Можливості Єви (що продаємо)

- **Обдзвін кандидатів голосом** (Vapi + Claude + укр. STT/TTS) за розкладом-слотами, гео/вік-скрин, підсумок кожного дзвінка.
- **Автозапис у CRM** — картка з транскриптом/аудіо/summary/стадією; кандидат зберігається як **покупець** (зелена галочка «клієнт»), поля Вакансія/Вік/Місто/Резюме заповнюються самі.
- **Telegram-переписка** реальним акаунтом (не бот) — персона, антибан, класифікація діалогу → CRM; live-транскрипт лягає в картку по ходу розмови (виживає видалення чату кандидатом).
- **Розумний handoff** — коли рекрутер бере кандидата (стадія «В роботі») Єва **більше не турбує**: мовчить у Telegram і не веде вхідний дзвінок (каже, що заявка вже в рекрутера).
- **Синхронізація з ручними діями рекрутера** — рекрутер зрушив картку/задиспозив у CRM → Єва це бачить і зупиняється (poll кожні 10хв + опційно KeyCRM-вебхук realtime).
- **Автовідповідач/оператор** — Єва розпізнає запис («абонент недоступний», voicemail) і мовчки кладе слухавку, не палить час/канал.
- **Inbound + outbound** з одного номера; вхідні маршрутизуються динамічно (`assistant-request`).
- **Мультиджерело кандидатів** — work.ua (офіційний API) + robota.ua, дедуп по телефону.

## 2. Акаунти + ключі (чекліст на нову компанію)

| Сервіс | Що взяти | Куди в .env |
|---|---|---|
| Anthropic | API key | `ANTHROPIC_API_KEY` |
| Vapi | API key, assistant_id, phoneNumberId | `VAPI_API_KEY`, `VAPI_ASSISTANT_ID`, `VAPI_PHONE_NUMBER_ID`, `VAPI_WEBHOOK_SECRET` |
| SIP-транк | логін/пароль/номер → створити Vapi credential + phone-number | (у Vapi, не в .env) |
| Deepgram | API key (мова STT) | `DEEPGRAM_API_KEY` |
| ElevenLabs | API key, voice_id | `ELEVENLABS_API_KEY` |
| CRM (KeyCRM) | API token, pipeline_id, статуси, кастомні поля | `KEYCRM_API_TOKEN`, `KEYCRM_BASE_URL` |
| work.ua | API token роботодавця | `WORKUA_*` |
| robota.ua | логін/пароль кабінету | `ROBOTAUA_EMPLOYER_EMAIL/PASSWORD` |
| Telegram userbot | окремий номер+SIM, api_id/api_hash (my.telegram.org) | `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` |
| Домен | api.<компанія>.com → A-запис на VPS | `APP_BASE_URL`, Caddyfile |

## 3. Конфігурація під компанію

1. `cp .env.example .env` — заповнити всі ключі вище.
2. **Вакансія/пітч** (`.env`): `COMPANY_NAME` (кирилицею для TTS!), `COMPANY_PITCH`, `DEFAULT_VACANCY_TITLE`, `DEFAULT_VACANCY_SALARY`, `DEFAULT_VACANCY_SCHEDULE`, `VACANCY_URL`, `KEYCRM_VACANCY_LABEL`, `VACANCY_NUMBER`.
   - ГРАБЛІ: латиниця в промпті = англ. вимова у TTS. Назви/абревіатури — кирилицею або фонетично.
3. **Гео-скрин** (`.env`): `REGION_WHITELIST`, `REGION_BLACKLIST`. Портрет віку — `PROFILE_AGE_MIN/MAX_F/M`.
4. **Голосовий скрипт** — `src/call/script_template.py` (структура розмови) + деплой у Vapi через `scripts/patch_eva.py`. Тайминги (endpointing/onPunctuation) — копіювати з робочого patch_eva.py, НЕ вигадувати.
5. **Персона ТГ** — `tg_userbot/persona.py`.
6. **CRM-воронка** — `src/common/keycrm_fields.py` `STAGE_MAP` (tech_key → status_id клієнтської воронки). Кастомні поля картки (Вік/Місто/Резюме/AI-поля) створюються РУКАМИ в UI CRM (API не дає) → код резолвить по імені (`keycrm.py:_resolve_extra_fields`).
7. **Режим лінка резюме** — `RESUME_LINK_MODE=workua|selfhosted`.

## 4. Деплой (VPS)

```bash
# на VPS
git clone <repo> /opt/ai-recruiter && cd /opt/ai-recruiter
cp .env.example .env && nano .env          # заповнити
cd deploy && docker compose build && docker compose up -d
```
- Прокинути домен на VPS, у `deploy/Caddyfile` вказати домен.
- Голос: `docker cp scripts/patch_eva.py deploy-api-1:/app/scripts/ && docker exec deploy-api-1 python /app/scripts/patch_eva.py`.
- Міграції: `alembic upgrade head` (або ручний ALTER для нових колонок).

## 4.5. Міграції БД (ОБОВʼЯЗКОВО на новій інсталяції)

`create_all()` створює лише відсутні ТАБЛИЦІ й НІКОЛИ не робить ALTER. Частину колонок
додавали руками на живому сервері — вони зібрані в міграції `0004`:

```bash
docker exec -e PYTHONPATH=/app -w /app deploy-api-1 alembic upgrade head
```

Міграція idempotent (`IF NOT EXISTS`) — безпечна і на порожній БД, і на вже пропатченій.
Якщо БД піднімалась через `create_all`, спершу проставте позначку:
`UPDATE alembic_version SET version_num='0003';`

## 5. Післядеплойний чекліст

- `curl https://api.<домен>/health` → `{"status":"ok"}`.
- Вебхук: `POST /webhooks/vapi/events` з `x-vapi-secret` → **200** (критично! після кожного rebuild api).
- Тест-дзвінок з `/menu` бота.
- Перевірити стадії воронки рухаються + кастомні поля заповнюються.
- Ліміти: `CALL_MAX_CONCURRENT` (= канали SIP), `CALL_MAX_ATTEMPTS`, `HARD_CALL_CAP`, TG `MAX_NEW_CONTACTS_PER_DAY=15` (антибан).
- **Гео**: `REGION_WHITELIST` — ЯВНИЙ перелік областей. Він же має збігатися з переліком у
  промптах (`script_template.allowed_regions`, `summarizer`, `_CLASSIFY_SYSTEM`, `persona`).
- **`TG_ADMIN_CHAT_IDS`** заповнити ОДРАЗУ — інакше Єва почне «співбесідувати» рекрутерів.
- `alembic upgrade head` виконано (див. 4.5).
- Telegram-хендлер живий: написати на акаунт Єви → має відповісти. Перевірити
  `docker logs deploy-tguserbot-1 | grep -i "unhandled\|TypeError"` → порожньо.

## 6. Ключові граблі (з досвіду Kozyr Trans)

- Вебхуки мертві після rebuild → caddy `dynamic a` + reconcile-job страховка.
- `fn()` у промпті без реєстрації в `model.tools` = мертвий текст (дзвінок висить). Факти — з summarizer.
- Кирилиця в HTTP-заголовках (User-Agent) = UnicodeEncodeError → ASCII fallback.
- Кастомні поля CRM створюй у UI (API POST = 405), код шукає по назві.
- SIP (підтверджено оператором 27.07): **480 = в абонента обмежена можливість приймати дзвінки** (немає коштів / за кордоном) — це НЕ вина лінії й НЕ вичерпані канали; 486 = зайнято. Разом 480+486 дають ~95% «фейлів» і є нормою. Реальні проблеми оператора — лише 503/403.
- Часові зони: `CronTrigger(timezone=...)` у КОЖНОМУ джобі, інакше UTC.

### Додано 2026-07-27 (дорого коштували)

- **Декоратор Telethon**: вставляючи функцію поряд з `@client.on(...)`, перевір, що
  декоратор лишився на своєму обробнику — `grep -n -B1 "^async def"`. Інакше
  handler мовчки не реєструється, і бот 3 доби не відповідає нікому.
- **`on_message` ловить лише ЖИВІ апдейти.** Все, що прийшло під час даунтайму,
  зникає назавжди → потрібен `catch_up_unread()` на старті (`TG_CATCHUP_ON_START=1`).
- **Ніколи не давай моделі географічну абстракцію** («правобережна», «південь»).
  Вона довигадує — Одесу віднесло до лівого берега й зарізало кандидата. Тільки перелік.
- **Порожній дзвінок (0с) може фіналізуватись ПІСЛЯ успішного** і затерти картку,
  кинути кандидата в Недозвін і надіслати «не змогли додзвонитися» тому, з ким щойно
  говорили. Guard: порожній transcript + існує реальна розмова → вийти.
- **Не рухай стадію картки, не звіривши живу.** Рекрутери працюють паралельно;
  масовий прогін легко відкотить їхні ручні рішення (`crm_stage_stop_status`).
- **Все, що робить Єва, має бути видно в картці** (дзвінків N/ліміт, причина
  людською мовою, чи пішов Telegram). Інакше «поговорили, добираємо в Telegram»
  висить місяцями поверх 13 невдалих дзвінків.
- **Telegram `PRIVACY_PREMIUM_REQUIRED`**: без Premium акаунт не може писати першим
  частині людей. Помилка не ловилась дефолтними except → падав увесь HTTP-запит.
- **Жіночий рід** у промптах треба задавати явними прикладами заборонених форм —
  модель дописує «Зрозумів» у власних преамбулах попри загальне правило.

---
Повна історія рішень і граблів — у пам'яті проєкту (`project_ai_recruiter`) та `ADMIN_GUIDE.md`.
