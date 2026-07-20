#!/usr/bin/env bash
# Initialize sops + age for encrypted .env management.
# Run once per VPS, as the non-root project user.

set -euo pipefail

KEYS_DIR="${HOME}/.config/sops/age"
mkdir -p "$KEYS_DIR"
chmod 700 "$KEYS_DIR"

if [[ ! -f "$KEYS_DIR/keys.txt" ]]; then
    echo "==> generating age key"
    age-keygen -o "$KEYS_DIR/keys.txt"
    chmod 600 "$KEYS_DIR/keys.txt"
fi

PUBKEY="$(grep 'public key:' "$KEYS_DIR/keys.txt" | awk '{print $NF}')"
echo "==> public age key: $PUBKEY"

cat > .sops.yaml <<EOF
creation_rules:
  - path_regex: \.env(\.\w+)?$
    encrypted_regex: '^(.*)$'
    age: $PUBKEY
EOF

echo ""
echo "==> usage:"
echo "    sops -e .env > .env.enc       # encrypt local plaintext .env"
echo "    sops -d .env.enc > .env       # decrypt on this VPS"
echo ""
echo "    Add .env.enc to git, NEVER commit .env."
