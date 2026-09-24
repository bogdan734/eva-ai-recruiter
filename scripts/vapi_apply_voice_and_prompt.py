"""Last two live changes: the compressed script, and the fastest TTS setting.

optimizeStreamingLatency 3 -> 4 is ElevenLabs' maximum. It buys roughly 0.1s at
the start of every sentence Eva speaks, and costs a little prosody -- which is
the honest trade, because the measurement says the remaining pause is in speech
synthesis, not in the model. If it sounds clipped on the test call, 3 is one
PATCH away.

The assistant-level prompt is updated to the compressed script too. Real calls
override it per candidate, so this is belt-and-braces: a call that somehow runs
without overrides now says the same thing as one that does.
"""
import json
import httpx

from src.call.script_template import render_system_prompt

KEY = AID = ''
for line in open('/app/.env'):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
    if line.startswith('VAPI_ASSISTANT_ID='):
        AID = line.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}

TEST_AID = '574db2a7-f6b3-4df6-a124-a44a36378fb3'
prompt = render_system_prompt()
print('compact prompt chars:', len(prompt))

for aid, label in ((AID, 'LIVE'), (TEST_AID, 'TEST')):
    cur = httpx.get(f'https://api.vapi.ai/assistant/{aid}', headers=H, timeout=30).json()
    voice = dict(cur.get('voice') or {})
    was = voice.get('optimizeStreamingLatency')
    voice['optimizeStreamingLatency'] = 4
    body = {
        'voice': voice,
        'model': {
            'provider': 'anthropic',
            'model': cur['model']['model'],
            'temperature': cur['model'].get('temperature', 0.4),
            'maxTokens': 75,
            'messages': [{'role': 'system', 'content': prompt}],
        },
    }
    r = httpx.patch(f'https://api.vapi.ai/assistant/{aid}', headers=H, json=body, timeout=60)
    print(f'{label} {aid[:8]}: PATCH {r.status_code}')
    if r.status_code >= 300:
        print(r.text[:800])
        continue
    n = r.json()
    print(f'  optimizeStreamingLatency: {was} -> {n["voice"]["optimizeStreamingLatency"]}')
    print(f'  prompt chars: {len(n["model"]["messages"][0]["content"])}'
          f' | maxTokens: {n["model"]["maxTokens"]}')
    print(f'  voice: {json.dumps(n["voice"], ensure_ascii=False)}')
