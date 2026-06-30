# Ringostat ↔ Vapi архітектура — research і рішення

## Питання
Ringostat вже стоїть у клієнта. Як його використати для AI-обзвону кандидатів?

## Що дає Ringostat API (з публічної доки + личного досвіду)

### Доступне через API
- ✅ **REST API**: статистика дзвінків, ініціація call-back (двосторонній дзвоник з підвʼязаним оператором), webhook'и про call events
- ✅ **Webhooks**: `call.started`, `call.ended`, з MP3-посиланням на запис
- ✅ **Click-to-Call**: ініціювати дзвоник на N1 → коли N1 підняв, дзвонити N2

### НЕ доступне напряму через API
- ❌ **Media Streams / real-time audio WebSocket** — у Ringostat НЕМАЄ нативного streaming API для real-time bot-dialogue
- ❌ **SIP trunk назовні** — Ringostat не дає SIP-trunk як white-label провайдер (це проксі-сервіс над операторами, а не sipprovider)
- ❌ **Прямий контроль над голосовим потоком**

### Висновок про Ringostat
Ringostat = система обліку дзвінків + click-to-call + аналітика, **НЕ telephony backend для AI voice agents**. Для real-time STT/TTS pipeline через Ringostat напряму — не вийде.

## Варіанти архітектури

### Варіант A: Vapi + Twilio як основна telephony. Ringostat — тільки для логів
```
Vapi.ai → Twilio (+380 номер) → кандидат
              │
              ▼ post-call webhook
        Наш бекенд
              │
              ├─▶ KeyCRM (картка)
              └─▶ Ringostat REST POST (запис дзвінка для аналітики менеджерів)
```

**Плюси:**
- Real-time WebSocket bridge готовий з коробки у Vapi
- Twilio Media Streams + Programmable Voice — стандарт індустрії
- Простий setup, працює за тиждень
- Latency 800–1200 мс end-to-end

**Мінуси:**
- Twilio +380 номер потребує верифікації бізнесу + A2P (1–2 тижні KYC)
- Ringostat-аналітика тільки після факту, не "live"
- Подвійний telephony cost: Twilio + Ringostat (якщо тримаємо обидва)

### Варіант B: Тільки Ringostat (без real-time)
**Реалізація:** Ringostat ініціює дзвоник на кандидата, але **бот** з боку Ringostat — це pre-recorded IVR з кнопками `1/2/3`. Без LLM-діалогу.

**Плюси:**
- Дешево, без Twilio
- Все в одній системі

**Мінуси:**
- ❌ НЕ real-time AI-діалог. Це інтерактивне меню.
- Не підходить для нашої задачі (HR-розмова з обробкою заперечень)

**Висновок:** не підходить. Відкидаємо.

### Варіант C: SIP trunk від українського оператора + Vapi
Купити SIP-trunk у Datagroup / Київстар Business / Lifecell Business → під'єднати до Vapi напряму через SIP.

**Плюси:**
- Локальний +380 номер без американського посередника
- Дешевше /хвилину (UA внутрішні ставки)
- Швидше KYC ніж у Twilio
- Той самий real-time pipeline через Vapi

**Мінуси:**
- Vapi підтримує custom SIP trunks, але треба перевіряти сумісність
- A2P-захист треба робити самим (Datagroup не дає anti-spam)
- Налаштування SIP складніше ніж Twilio drop-in

### Варіант D (гібрид, рекомендований): Twilio для AI-дзвінків + Ringostat для людських дзвінків
```
AI-обзвон (cold outbound):
  Vapi → Twilio +380 → кандидат → запис + transcript → KeyCRM

Менеджерські дзвінки (warm follow-up після AI):
  Ringostat (як є) → менеджер дзвонить вручну
```

Менеджер бачить в KeyCRM AI-card з summary і дзвонить через свій звичний Ringostat. Дві системи живуть паралельно, не конфліктують.

## Рекомендоване рішення: Варіант A → еволюція в C

### Phase 1 (MVP, тиждень 1–2)
**Vapi + Twilio +380.** Real-time pipeline, перевірена комбінація. Запускаємо швидко.

### Phase 2 (якщо обсяг >500 дзвінків/день, місяць 2)
Переходимо на **SIP-trunk Datagroup → Vapi**, економимо $0.02–0.04/хв.

### Phase 3 (опц.)
Власний WebSocket-міст без Vapi, повний контроль над пайплайном. Тільки якщо обсяг >2000 дзвінків/день.

## Що залишається за Ringostat
- Аналітика дзвінків менеджерів (warm follow-up)
- Click-to-call з KeyCRM-картки (менеджер натискає → дзвонить через Ringostat)
- Запис менеджерських дзвінків в єдиному місці

**AI-обзвон Ringostat НЕ виконує.** Точка.

## Вимоги до Twilio setup

- [ ] Twilio акаунт з верифікацією бізнесу (статут компанії, посвідчення директора)
- [ ] Купівля +380 номера (~$6/міс) — потребує UA address proof
- [ ] A2P-реєстрація (обовʼязкова для outbound масових дзвінків): 3–7 днів
- [ ] Media Streams включити (TwiML `<Stream>` параметр)
- [ ] Webhook URL: `https://webhooks.recruiter-ai.example.com/twilio/voice`

## Вартість Phase 1 (на 200 дзвінків × 3 хв)

| Стаття | $/міс |
|---|---|
| Twilio +380 номер | $6 |
| Twilio Voice outbound (600 хв × $0.04) | $24 |
| Vapi orchestration (600 хв × $0.05) | $30 |
| Deepgram STT (600 хв × $0.0043) | $3 |
| ElevenLabs TTS | $22 |
| Claude (включаючи post-call summary) | ~$30 |
| **Telephony+AI** | **~$115** |

## Чек-ліст рішень для тебе

- [ ] **Підтверджуєш варіант A (Vapi + Twilio) для Phase 1?**
- [ ] Хочемо паралельно подати заявку на SIP-trunk Datagroup для Phase 2?
- [ ] Згоден що Ringostat залишається для менеджерських дзвінків, AI на Twilio?
- [ ] KYC для Twilio готовий зробити? (статут компанії, документи директора)
