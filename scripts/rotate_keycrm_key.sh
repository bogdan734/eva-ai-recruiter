#!/usr/bin/env bash
# Rotate KEYCRM_API_TOKEN without the key ever touching a chat log or shell history.
#
# KeyCRM issues ONE key per account for every integration at once, so pressing
# the refresh button in the cabinet kills the previous key permanently. That is
# the point here: the foreign integration on 91.206.200.150 dies with it and the
# duplicate cards stop. Everything that key fed us stops too — see the handoff.
#
#   ./scripts/rotate_keycrm_key.sh --check     verify the key currently in .env
#   ./scripts/rotate_keycrm_key.sh             prompt for a new key and deploy it
set -euo pipefail

cd /opt/ai-recruiter

verify() {
    local label="$1"
    docker compose -f deploy/docker-compose.yml exec -T scheduler python - <<'PY'
import asyncio, sys
from src.common.keycrm import KeyCRMClient

async def main():
    c = KeyCRMClient()
    ok = True
    r = await c._get_rate_limited("/pipelines", params={"limit": 1})
    print(f"  GET /pipelines        -> {r.status_code}")
    ok &= r.status_code == 200
    r = await c._get_rate_limited("/pipelines/cards", params={"limit": 1, "filter[pipeline_id]": 6})
    print(f"  GET /pipelines/cards  -> {r.status_code}")
    ok &= r.status_code == 200
    await c.aclose()
    sys.exit(0 if ok else 1)

asyncio.run(main())
PY
    local rc=$?
    if [ $rc -eq 0 ]; then
        echo "  ✅ $label: ключ робочий"
    else
        echo "  ❌ $label: ключ НЕ працює"
    fi
    return $rc
}

if [ "${1:-}" = "--check" ]; then
    echo "Перевірка поточного ключа:"
    verify "поточний"
    exit $?
fi

echo "Перевірка ключа ДО заміни:"
verify "до заміни" || echo "  (попередження: старий ключ уже не працює)"
echo

# -s so it never lands in the terminal or in shell history.
read -rsp "Новий ключ KeyCRM (вставити і Enter): " NEW_KEY
echo
if [ -z "$NEW_KEY" ]; then
    echo "Порожній ключ — нічого не змінено."
    exit 1
fi

BACKUP=".env.bak-$(date +%Y%m%d-%H%M%S)-keyrotate"
cp .env "$BACKUP"
echo "Бекап .env → $BACKUP"

# python, not sed: an API key may contain characters sed would treat as syntax.
NEW_KEY="$NEW_KEY" python3 - <<'PY'
import os, pathlib

key = os.environ["NEW_KEY"].strip()
p = pathlib.Path(".env")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
found = False
for i, line in enumerate(lines):
    if line.startswith("KEYCRM_API_TOKEN="):
        lines[i] = f"KEYCRM_API_TOKEN={key}\n"
        found = True
        break
if not found:
    raise SystemExit("KEYCRM_API_TOKEN not found in .env — nothing written")
p.write_text("".join(lines), encoding="utf-8")
print(f"  .env оновлено ({len(key)} символів)")
PY
unset NEW_KEY

# env_file is read at container create, so `up -d` is required. No build: the
# key is configuration, not code.
echo "Перезапуск сервісів..."
docker compose -f deploy/docker-compose.yml up -d api bot scheduler
sleep 12

echo
echo "Перевірка ключа ПІСЛЯ заміни:"
if verify "новий"; then
    echo
    echo "Готово. Чужа інтеграція (91.206.200.150) від цього моменту мертва."
    echo "Далі: подивитись за добу, що нових карток від manager 3 без наших"
    echo "keycrm_lead_id більше не з'являється."
else
    echo
    echo "⚠️ Новий ключ не проходить. Відкотити:"
    echo "   cp $BACKUP .env && docker compose -f deploy/docker-compose.yml up -d api bot scheduler"
    exit 1
fi
