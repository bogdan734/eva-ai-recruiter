# KeyCRM Schema — AI Recruiter funnel

Доку для копіпасту в KeyCRM при налаштуванні воронки і кастомних полів.

## Воронка

**Назва:** `AI Recruiter — {VACANCY_NAME}`
(на кожну вакансію — окрема воронка АБО одна загальна з полем `vacancy_id`. Для тесту — одна загальна.)

### Етапи (по порядку)

| # | Назва етапу | Внутр. ключ | Опис |
|---|---|---|---|
| 1 | 🆕 Нові резюме | `new_resume` | Зайшли зі скрапера або Apix-відгуку. Ще не оброблені фільтрами. |
| 2 | 🎯 Пройшли фільтр | `filtered` | Регіон-фільтр + embedding-match ≥ поріг. Готові до дзвінка. |
| 3 | 📞 В черзі дзвінків | `in_call_queue` | Очікують свій слот (9–19 кожні 2 год). |
| 4 | 🔁 Дозвонюємось | `calling` | Спроба 1/3, 2/3, 3/3. |
| 5 | ⛔ Не дозвонились | `unreachable` | Вичерпано 3 спроби. |
| 6 | 💬 Дзвінок відбувся | `call_done` | Розбираємо результат: AI-summary, transcript. |
| 7 | ⭐ У менеджера | `manager_review` | Кваліфіковано → передано HR-менеджеру. |
| 8 | ✅ Інтервʼю заплановане | `interview_scheduled` | Менеджер призначив дату/час. |
| 9 | 🏁 Закрито | `closed` | Найм або відмова. |

### Бічні гілки (теги, не окремі етапи)
- `❌ Не підходить` — на етапі 6, якщо AI-аналіз показав не-метч (наприклад, кандидат відмовився)
- `🚫 Blacklist` — agressive / forbidden / repetitive guardrail trip
- `🔄 Перетелефонувати` — кандидат сам попросив call-back

## Кастомні поля на картці ліда

Створювати через `Settings → Custom Fields → Leads`.

| # | Назва поля | Тех. ключ | Тип | Обовʼязкове | Default | Джерело |
|---|---|---|---|---|---|---|
| 1 | URL резюме work.ua | `work_ua_url` | URL | ні | — | scraper / Apix |
| 2 | Регіон | `region` | Select | так | — | scraper |
| 3 | Бажана посада | `desired_position` | Text | ні | — | scraper |
| 4 | Досвід (років) | `experience_years` | Number | ні | 0 | scraper |
| 5 | Мови | `languages` | Multi-select (uk, ru, en, pl, de, fr, other) | ні | — | scraper |
| 6 | Match score | `match_score` | Number 0–100 | ні | — | embedding |
| 7 | Vacancy ID | `vacancy_id` | Number/Ref | так | — | orchestrator |
| 8 | Спроб дзвінка | `call_attempts` | Number | ні | 0 | scheduler |
| 9 | Останній дзвінок | `last_call_at` | DateTime | ні | — | Vapi webhook |
| 10 | Статус останнього дзвінка | `last_call_status` | Select (success, no_answer, busy, voicemail, hangup, blocked) | ні | — | Vapi |
| 11 | Тривалість дзвінка (сек) | `last_call_duration_sec` | Number | ні | 0 | Vapi |
| 12 | Аудіо запис | `audio_url` | URL | ні | — | S3 signed |
| 13 | Транскрипт | `transcript` | Long Text | ні | — | Deepgram |
| 14 | AI-резюме (3 буліти) | `ai_summary` | Long Text | ні | — | Claude post-call |
| 15 | Sentiment | `sentiment` | Select (positive, neutral, negative) | ні | — | Claude |
| 16 | Заперечення | `objections_raised` | Multi-select (distance, salary, timing, field, current_job, other) | ні | — | Claude |
| 17 | Мова розмови | `language_used` | Select (uk, ru, en, mixed) | ні | — | Deepgram |
| 18 | Tokens in | `tokens_input` | Number | ні | 0 | Vapi log |
| 19 | Tokens out | `tokens_output` | Number | ні | 0 | Vapi log |
| 20 | Вартість дзвінка USD | `cost_usd` | Number (2 decimals) | ні | 0 | calc |
| 21 | Теги | `tags` | Multi-select (qualified, aggressive, repetitive, blocked, callback, vip) | ні | — | guardrails |
| 22 | Менеджер | `manager_assigned` | User Reference | ні | — | handoff |
| 23 | Дата інтервʼю | `interview_scheduled_at` | DateTime | ні | — | manager |
| 24 | Source | `source` | Select (workua_inbound, workua_scraper, robota_api, manual) | так | — | router |

## Webhooks

Налаштувати в KeyCRM: `Settings → Integrations → Webhooks`.

| Подія | URL |
|---|---|
| `lead.created` | `https://webhooks.recruiter-ai.example.com/keycrm/lead-created` |
| `lead.stage_changed` | `https://webhooks.recruiter-ai.example.com/keycrm/lead-stage` |
| `lead.assigned` | `https://webhooks.recruiter-ai.example.com/keycrm/lead-assigned` |
| `lead.field_updated` | `https://webhooks.recruiter-ai.example.com/keycrm/lead-field` |

Підпис: `X-KeyCRM-Signature: <HMAC-SHA256>` (секрет в `KEYCRM_WEBHOOK_SECRET`).

## API endpoints (KeyCRM REST)

Шпаргалка по methods які використовуємо:

| Метод | Опис | Endpoint |
|---|---|---|
| POST | Створити ліда | `/v1/pipelines/{funnel_id}/cards` |
| PUT | Оновити поля | `/v1/pipelines/{funnel_id}/cards/{lead_id}` |
| PUT | Перевести етап | `/v1/pipelines/{funnel_id}/cards/{lead_id}/move` |
| GET | Знайти за телефоном | `/v1/leads?search={phone}` |
| GET | Список ділянок воронки | `/v1/pipelines/{funnel_id}/stages` |

## Дедуплікація

Перед створенням нового ліда — пошук за телефоном (нормалізованим до E.164):
```python
existing = await keycrm.find_by_phone(phone_e164)
if existing:
    await keycrm.update_fields(existing.id, source_tag=incoming.source, ...)
else:
    await keycrm.create_lead(...)
```

## Стартовий чек-лист налаштування (день 3)

- [ ] Створити воронку `AI Recruiter — Test`
- [ ] Додати 9 етапів
- [ ] Створити 24 кастомних поля
- [ ] Згенерувати API-токен з правами `leads:write`
- [ ] Налаштувати 4 webhook'и
- [ ] Створити 1 тестового ліда через UI — перевірити що всі поля видно
- [ ] Створити 1 тестового ліда через API — перевірити що webhook прийшов
- [ ] Документувати `funnel_id` у `.env`
