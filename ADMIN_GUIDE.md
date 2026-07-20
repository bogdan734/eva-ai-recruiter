# Єва (AI-рекрутер Kozyr Trans) — інструкція для адмінів

Оновлено: 2026-07-20

## Що це і де живе

| Компонент | Де | Що робить |
|---|---|---|
| Сервер (VPS) | `ssh root@65.21.151.71`, код у `/opt/ai-recruiter` | все нижче крутиться тут у Docker |
| `api` | контейнер `deploy-api-1` | приймає події дзвінків від Vapi, аналізує розмови, шле анкети |
| `bot` | `deploy-bot-1` | Telegram-панель керування **@KozyrTransHRBot** |
| `scheduler` | `deploy-scheduler-1` | обдзвін черги по слотах 09/11/13/15/17/19 (Київ) + збір кандидатів з work.ua кожні 5 хв |
| `tguserbot` | `deploy-tguserbot-1` | ТГ-акаунт Єви (@HR_KozyrTrans) — авто-відповіді кандидатам, відправка анкет |
| `db`, `caddy` | | база та веб-проксі — не чіпати |
| `scraper` | ЗУПИНЕНИЙ навмисно | старий, не потрібен — кандидати йдуть через work.ua API |
| Голос/дзвінки | vapi.ai, асистент `88ecfb7b-…` | мозок і голос Єви |
| Телефонія | Stream Telecom SIP, номер 044-300-23-08 (тестовий, ліміт 50 хв) | вхідні + вихідні |

## Панель керування (для всіх адмінів)

Telegram → **@KozyrTransHRBot** → `/menu`. Доступ мають: Богдан, Тетяна, Світлана, Артем.

- ▶️ / ⏸ — запустити / поставити на паузу обдзвін (зараз ПАУЗА до бойового номера!)
- 📞 — тестовий дзвінок на будь-який номер
- 🎯 — критерії відбору кандидатів (регіони, вік, поріг)
- 💼 — параметри вакансії
- 📨 Telegram Єва — керування ТГ-розсилкою (стоп/старт, ліміт, статистика)
- 📥 — ручний імпорт кандидатів

**90% питань вирішується з панелі. У сервер лізти — тільки якщо панель не допомогла.**

## Якщо щось зламалось (по черзі)

1. **Бот не відповідає в ТГ / Єва не дзвонить / не відповідає в ТГ-чатах:**
   ```bash
   ssh root@65.21.151.71
   cd /opt/ai-recruiter/deploy
   docker compose ps        # що впало? (має бути Up у всіх, крім scraper)
   docker compose restart bot          # або: api / scheduler / tguserbot
   ```
2. **Подивитись, що пише сервіс:**
   ```bash
   docker logs deploy-bot-1 --tail 50        # bot / api / scheduler / tguserbot
   ```
3. **Перезапуск усього (безпечно, ~30 сек):**
   ```bash
   cd /opt/ai-recruiter/deploy && docker compose restart
   ```
4. **Сервер завис повністю:** перезавантажити VPS у панелі Hetzner — після старту все піднімається саме.

## Дзвінки: пауза/запуск без панелі (аварійно)

Файл `/opt/ai-recruiter/state/ai_recruiter_state.json`:
`"calls_paused": true` — стоп, `false` — працює. Зміна діє одразу, рестарт не потрібен.

## Зміни голосу/скрипта Єви (тільки той, хто розуміє, що робить)

- Текст скрипта: `/opt/ai-recruiter/src/call/script_template.py`
- Після зміни ТІЛЬКИ тексту:
  ```bash
  cd /opt/ai-recruiter/deploy && docker compose build api bot scheduler && docker compose up -d api bot scheduler
  docker exec deploy-api-1 mkdir -p /app/scripts
  docker cp /opt/ai-recruiter/scripts/patch_eva.py deploy-api-1:/app/scripts/patch_eva.py
  docker exec deploy-api-1 python /app/scripts/patch_eva.py
  ```
- Голос/таймінги/привітання: `/opt/ai-recruiter/scripts/patch_eva.py` (потім ті самі 3 команди вище).

### Відкат швидкості реакцій (якщо Єва перебиває / обриває себе)

Зараз стоять ЕКСПЕРИМЕНТАЛЬНО мінімальні паузи. Повернути перевірені:
```bash
cp /opt/ai-recruiter/scripts/patch_eva.py.stable-20260720 /opt/ai-recruiter/scripts/patch_eva.py
docker cp /opt/ai-recruiter/scripts/patch_eva.py deploy-api-1:/app/scripts/patch_eva.py
docker exec deploy-api-1 python /app/scripts/patch_eva.py
```

### Відкат будь-якої зміни коду

Код під git: `cd /opt/ai-recruiter && git log --oneline` → `git checkout <хеш> -- шлях/до/файлу`, далі rebuild потрібного сервісу.

## Авто-анкета (нове з 20.07)

Якщо в дзвінку Єва пообіцяла надіслати анкету — після дзвінка ТГ-Єва сама пише кандидату
посилання на форму. Перевірити, що спрацювало:
```bash
docker logs deploy-api-1 2>&1 | grep anketa_form
```
Посилання на форму задається змінною `ANKETA_FORM_URL` у `/opt/ai-recruiter/.env`.

## Що НЕ робити

- Не запускати обдзвін (▶️), поки лінія тестова (50 хв ліміт) — чекаємо бойовий номер.
- Не міняти таймінги в patch_eva.py нижче за stable-значення без тестів.
- Не видаляти файли `*.bak-*` і `patch_eva.py.stable-*` — це точки відкату.
- Не чіпати `db`, `caddy`, `.env` без розуміння.
- `docker compose up -d scraper` — НЕ запускати, він вимкнений свідомо.

## Коли з'явиться бойовий номер Stream Telecom

1. У Vapi: оновити/створити phoneNumber на credential `4eb18c50-…` з новими SIP-даними.
2. У `/opt/ai-recruiter/.env`: `VAPI_PHONE_NUMBER_ID=<новий id>`.
3. `cd deploy && docker compose up -d bot scheduler api`
4. Тест-дзвінок з панелі → якщо ок → ▶️ зняти паузу.
