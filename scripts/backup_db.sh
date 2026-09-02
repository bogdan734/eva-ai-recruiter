#!/usr/bin/env bash
# Nightly dump of the candidate database.
#
# There were no backups at all until 2026-09-01, which came uncomfortably close
# to mattering: an unpaid $23.59 invoice put the account at Hetzner's warning
# level 3, one step short of the server being deleted with everything on it.
#
# What this does and does not cover is worth being honest about. It protects
# against a bad migration, a wrong DELETE, a corrupted volume — the things that
# actually happen. It does NOT protect against losing the account, because the
# dumps live on the same disk. Off-site copies are a separate decision; paying
# the invoice is the real defence against that one.
#
# Failure has to be loud. A backup that quietly stopped running is worse than no
# backup, because it buys false confidence — so every run writes its outcome to
# state/backup_status.json and the daily digest reads it back.
set -uo pipefail

cd /opt/ai-recruiter || exit 1

DEST="backups"
STATUS="state/backup_status.json"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M)"
FILE="$DEST/recruiter-$STAMP.sql.gz"

mkdir -p "$DEST" state

write_status() {
    # Written on every path, success or failure — the digest reports whatever is
    # here, and a stale timestamp is itself the alarm.
    printf '{\n  "ok": %s,\n  "at": "%s",\n  "file": "%s",\n  "bytes": %s,\n  "error": "%s"\n}\n' \
        "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" "$3" "$4" > "$STATUS"
}

err="$(cd deploy && docker compose exec -T db pg_dump -U recruiter -d recruiter 2>&1 >/tmp/dump.sql)"
rc=$?
if [ $rc -ne 0 ] || [ ! -s /tmp/dump.sql ]; then
    rm -f /tmp/dump.sql
    write_status false "" 0 "$(echo "$err" | tail -1 | tr '"' "'" | cut -c1-200)"
    echo "backup FAILED: $err"
    exit 1
fi

gzip -c /tmp/dump.sql > "$FILE"
rm -f /tmp/dump.sql

size="$(stat -c %s "$FILE" 2>/dev/null || echo 0)"
# A dump that is suspiciously small is a failure wearing a success costume:
# pg_dump exits 0 for an empty database too.
if [ "$size" -lt 10000 ]; then
    write_status false "$FILE" "$size" "dump too small — $size bytes"
    echo "backup SUSPICIOUS: only $size bytes"
    exit 1
fi

find "$DEST" -name 'recruiter-*.sql.gz' -mtime "+$KEEP_DAYS" -delete 2>/dev/null

write_status true "$FILE" "$size" ""
echo "backup ok: $FILE ($size bytes), kept ${KEEP_DAYS}d"
