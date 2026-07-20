#!/usr/bin/env bash
# Zadarma outbound SIP trunk -> Vapi. Backup config, ready to run.
# Fill the 3 vars from Zadarma cabinet (Settings -> SIP), then run.
#
#   ZADARMA_SIP_LOGIN   e.g. 123456 (your SIP account / номер)
#   ZADARMA_SIP_PASS    SIP password from cabinet
#   ZADARMA_SIP_HOST    usually sip.zadarma.com  (some accounts: pbx.zadarma.com)
#
# CLI stays the verified UA number +380673350196.
set -euo pipefail

VAPI_KEY="3096f9ba-9f2b-4446-8512-7a84903996e8"
ASSISTANT="88ecfb7b-b732-41e9-83e6-cafee118a436"
CLI_NUMBER="+380673350196"

ZADARMA_SIP_LOGIN="${ZADARMA_SIP_LOGIN:-FILL_ME}"
ZADARMA_SIP_PASS="${ZADARMA_SIP_PASS:-FILL_ME}"
ZADARMA_SIP_HOST="${ZADARMA_SIP_HOST:-sip.zadarma.com}"

if [[ "$ZADARMA_SIP_LOGIN" == "FILL_ME" ]]; then
  echo "Fill ZADARMA_SIP_LOGIN / ZADARMA_SIP_PASS / ZADARMA_SIP_HOST first."; exit 1
fi

HOST_IP=$(dig +short "$ZADARMA_SIP_HOST" | tail -1)
echo "resolved $ZADARMA_SIP_HOST -> $HOST_IP"

# 1. credential (byo-sip-trunk, register auth)
CRED_ID=$(curl -s -X POST https://api.vapi.ai/credential \
  -H "Authorization: Bearer $VAPI_KEY" -H "Content-Type: application/json" \
  -d "{
    \"provider\": \"byo-sip-trunk\",
    \"name\": \"Zadarma outbound\",
    \"gateways\": [{\"ip\": \"$HOST_IP\", \"port\": 5060}],
    \"outboundAuthenticationPlan\": {
      \"authUsername\": \"$ZADARMA_SIP_LOGIN\",
      \"authPassword\": \"$ZADARMA_SIP_PASS\",
      \"sipRegisterPlan\": {\"realm\": \"$ZADARMA_SIP_HOST\"}
    },
    \"outboundLeadingPlusEnabled\": true
  }" | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")
echo "credential id: $CRED_ID"

# 2. phone number on this trunk, showing the verified UA CLI
PHONE_ID=$(curl -s -X POST https://api.vapi.ai/phone-number \
  -H "Authorization: Bearer $VAPI_KEY" -H "Content-Type: application/json" \
  -d "{
    \"provider\": \"byo-phone-number\",
    \"name\": \"UA Zadarma CLI\",
    \"number\": \"$CLI_NUMBER\",
    \"numberE164CheckEnabled\": true,
    \"credentialId\": \"$CRED_ID\"
  }" | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")
echo "phone id: $PHONE_ID"

echo
echo "NEXT:"
echo "  test:   curl -X POST https://api.vapi.ai/call -H 'Authorization: Bearer $VAPI_KEY' -H 'Content-Type: application/json' -d '{\"phoneNumberId\":\"$PHONE_ID\",\"assistantId\":\"$ASSISTANT\",\"customer\":{\"number\":\"+380XXXXXXXXX\"}}'"
echo "  bot:    ssh root@65.21.151.71 \"cd /opt/ai-recruiter && sed -i 's|^VAPI_PHONE_NUMBER_ID=.*|VAPI_PHONE_NUMBER_ID=$PHONE_ID|' .env && cd deploy && docker compose up -d bot\""
echo "  add to menu _TRUNKS in src/bot/menu.py: \"zadarma\": (\"$PHONE_ID\", \"Zadarma\")"
