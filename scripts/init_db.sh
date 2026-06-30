#!/usr/bin/env bash
# Apply Alembic migrations to the configured DATABASE_URL.
# Usage: ./scripts/init_db.sh

set -euo pipefail

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found. Decrypt with: sops -d .env.enc > .env" >&2
    exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

alembic upgrade head
echo "migrations applied"
