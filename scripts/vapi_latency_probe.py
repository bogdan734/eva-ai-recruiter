"""What the candidate actually hears: silence before Eva's first word, and
how long every later turn makes them wait.

Vapi reports timings two ways. The transcript messages carry secondsFromStart,
which is wall-clock from the moment the call object was created -- that includes
ringing, so it is the number the CANDIDATE experiences. performanceMetrics (when
present) breaks a turn into transcriber/model/voice, which is the number we can
actually shorten.
"""
import asyncio, json, os, sys
import httpx

KEY = ''
for line in open(os.environ.get('ENV_FILE','/app/.env')):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()

IDS = sys.argv[1:]


async def main():
    async with httpx.AsyncClient(timeout=30, headers={'Authorization': f'Bearer {KEY}'}) as c:
        for cid in IDS:
            r = await c.get(f'https://api.vapi.ai/call/{cid}')
            if r.status_code != 200:
                print(cid, 'HTTP', r.status_code, r.text[:200]); continue
            d = r.json()
            print('=' * 70)
            print('call', cid, '|', d.get('status'), '|', d.get('endedReason'))
            for k in ('createdAt', 'startedAt', 'endedAt'):
                print(f'  {k}: {d.get(k)}')
            cb = d.get('costBreakdown') or {}
            print('  costBreakdown keys:', {k: v for k, v in cb.items() if 'Token' in k or 'llm' in k.lower()})
            pm = d.get('performanceMetrics')
            print('  performanceMetrics:', json.dumps(pm, ensure_ascii=False)[:600] if pm else None)
            msgs = d.get('messages') or []
            print(f'  messages: {len(msgs)}')
            for m in msgs[:12]:
                role = m.get('role')
                sfs = m.get('secondsFromStart')
                dur = m.get('duration')
                txt = (m.get('message') or '')[:70].replace('\n', ' ')
                print(f'    {str(sfs):>8}s  dur={str(dur):>7}  {role:<9} {txt}')
            an = d.get('analysis') or {}
            if an:
                print('  analysis keys:', list(an.keys()))

asyncio.run(main())
