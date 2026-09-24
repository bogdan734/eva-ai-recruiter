"""Step 0 + step 2: make the region switchable, then try nova-3 on the copy.

Step 0 exists so that step 1 (the EU move) is a one-line change and a one-line
rollback. Right now "https://api.vapi.ai" is hardcoded in three places that
matter -- the client, the dispatcher and the recordings proxy -- so moving
region would mean editing code under time pressure with calls in flight. After
this it is VAPI_BASE_URL in .env, and putting it back is the rollback.

Step 2 is nova-2 -> nova-3 for Deepgram. Verified against Vapi's own schema:
nova-3 is in the accepted model list and `uk` is one of its 89 languages.
Deepgram's docs put nova-3 at a 54% lower word error rate in streaming than
competitors, and Ukrainian is supported on both models -- so this is an
accuracy change, not a latency one. Better recognition means fewer re-asks,
and a re-ask costs a candidate far more seconds than any endpointing tweak.

Applied to the TEST assistant only. The live one is not touched until a call
has been heard.
"""
import json
import pathlib
import httpx

# --------------------------------------------------- rollback snapshot first
KEY = AID = ''
for line in open('/app/.env'):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
    if line.startswith('VAPI_ASSISTANT_ID='):
        AID = line.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}
US = 'https://api.vapi.ai'

snapshot = {}
for path in ('assistant', 'phone-number', 'credential'):
    r = httpx.get(f'{US}/{path}', headers=H, timeout=45)
    snapshot[path] = r.json() if r.status_code == 200 else {'error': r.status_code}
out = pathlib.Path('/app/state/vapi_us_snapshot_20260923.json')
out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))
print('US snapshot ->', out, f'({out.stat().st_size} bytes)')
print('  assistants:', len(snapshot['assistant']),
      '| numbers:', len(snapshot['phone-number']),
      '| credentials:', len(snapshot['credential']))

# ------------------------------------------- step 0: region becomes a setting
s = pathlib.Path('/app/src/common/settings.py')
t = s.read_text()
OLD_S = '''    vapi_phone_number_id: str = ""  # Vapi phone-number id used for outbound (Zadarma trunk)'''
NEW_S = '''    vapi_phone_number_id: str = ""  # Vapi phone-number id used for outbound (Stream Telecom trunk)
    # Vapi regions are isolated environments: an organization belongs to one,
    # and assistants, numbers and SIP credentials live with it. Moving to the
    # EU region is therefore a different base URL AND a different key -- kept
    # here so the move, and the move back, are one line in .env rather than an
    # edit to three source files while calls are in flight.
    #   US: https://api.vapi.ai    (sip.vapi.ai)
    #   EU: https://api.eu.vapi.ai (sip.eu.vapi.ai)
    vapi_base_url: str = "https://api.vapi.ai"'''
assert t.count(OLD_S) == 1, 'settings anchor not found exactly once'
s.write_text(t.replace(OLD_S, NEW_S, 1))
print('patched settings.py (vapi_base_url)')

c = pathlib.Path('/app/src/call/vapi_client.py')
t = c.read_text()
OLD_C = 'def __init__(self, token: str | None = None, base_url: str = "https://api.vapi.ai") -> None:'
NEW_C = 'def __init__(self, token: str | None = None, base_url: str | None = None) -> None:'
assert t.count(OLD_C) == 1, 'vapi_client signature not found exactly once'
t = t.replace(OLD_C, NEW_C, 1)
# The body has to resolve the default now that the signature no longer does.
marker = NEW_C + '\n'
i = t.find(marker) + len(marker)
indent = '        '
t = (t[:i] + indent + 'base_url = base_url or get_settings().vapi_base_url\n' + t[i:])
if 'from src.common.settings import get_settings' not in t:
    t = t.replace('\n\n', '\nfrom src.common.settings import get_settings\n\n', 1)
c.write_text(t)
print('patched vapi_client.py')

d = pathlib.Path('/app/src/scheduler/dispatcher.py')
t = d.read_text()
OLD_D = '        base_url="https://api.vapi.ai",'
assert t.count(OLD_D) == 1, 'dispatcher base_url not found exactly once'
d.write_text(t.replace(OLD_D, '        base_url=s.vapi_base_url,', 1))
print('patched dispatcher.py')

m = pathlib.Path('/app/src/api/main.py')
t = m.read_text()
OLD_M = '            f"https://api.vapi.ai/call/{vapi_call_id}",'
assert t.count(OLD_M) == 1, 'main.py recordings url not found exactly once'
m.write_text(t.replace(OLD_M, '            f"{get_settings().vapi_base_url}/call/{vapi_call_id}",', 1))
print('patched main.py')

# ------------------------------------------------- step 2: nova-3 on the copy
TEST_AID = '574db2a7-f6b3-4df6-a124-a44a36378fb3'
cur = httpx.get(f'{US}/assistant/{TEST_AID}', headers=H, timeout=30).json()
tr = dict(cur.get('transcriber') or {})
print('\ntranscriber before:', json.dumps(tr, ensure_ascii=False))
tr['model'] = 'nova-3'
r = httpx.patch(f'{US}/assistant/{TEST_AID}', headers=H, json={'transcriber': tr}, timeout=60)
print('PATCH test assistant:', r.status_code)
print('transcriber after :', json.dumps(r.json().get('transcriber'), ensure_ascii=False)
      if r.status_code < 300 else r.text[:600])
