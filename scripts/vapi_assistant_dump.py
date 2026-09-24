"""What the live assistant is actually configured to do -- read only."""
import asyncio, json, os
import httpx

KEY = AID = ''
for line in open(os.environ.get('ENV_FILE', '/app/.env')):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
    if line.startswith('VAPI_ASSISTANT_ID='):
        AID = line.split('=', 1)[1].strip()


async def main():
    async with httpx.AsyncClient(timeout=30, headers={'Authorization': f'Bearer {KEY}'}) as c:
        r = await c.get('https://api.vapi.ai/assistant')
        alist = r.json()
        print('assistants in account:', len(alist))
        for a in alist:
            print('  ', a.get('id'), '|', a.get('name'), '| live' if a.get('id') == AID else '')
        print()
        r = await c.get(f'https://api.vapi.ai/assistant/{AID}')
        d = r.json()
        print('=== LIVE ASSISTANT', AID, '===')
        for k in ('name', 'firstMessage', 'firstMessageMode', 'silenceTimeoutSeconds',
                  'responseDelaySeconds', 'llmRequestDelaySeconds', 'numWordsToInterruptAssistant',
                  'maxDurationSeconds', 'backgroundSound', 'startSpeakingPlan', 'stopSpeakingPlan'):
            if k in d:
                print(f'{k}: {json.dumps(d[k], ensure_ascii=False)}')
        m = d.get('model') or {}
        print('model:', json.dumps({k: v for k, v in m.items() if k != 'messages'}, ensure_ascii=False)[:1500])
        msgs = m.get('messages') or []
        for mm in msgs:
            content = mm.get('content') or ''
            print(f"  systemPrompt role={mm.get('role')} chars={len(content)}")
        v = d.get('voice') or {}
        print('voice:', json.dumps(v, ensure_ascii=False)[:400])
        t = d.get('transcriber') or {}
        print('transcriber:', json.dumps(t, ensure_ascii=False)[:400])

asyncio.run(main())
