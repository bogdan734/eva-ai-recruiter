"""Place one test call on a given assistant and report what the human heard.

Prints the same two numbers measured on the live calls -- silence before Eva's
first word, and the gap after every candidate turn -- so the test is comparable
to the 15-22.09 baseline rather than to a feeling.
"""
import asyncio, json, os, sys, time
import httpx

KEY = PHONE_ID = ''
for l in open('/app/.env'):
    if l.startswith('VAPI_API_KEY='):
        KEY = l.split('=', 1)[1].strip()
    if l.startswith('VAPI_PHONE_NUMBER_ID='):
        PHONE_ID = l.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}

ASSISTANT = sys.argv[1]
NUMBER = sys.argv[2]
LABEL = sys.argv[3] if len(sys.argv) > 3 else 'test'


async def main():
    async with httpx.AsyncClient(timeout=60, headers=H) as c:
        r = await c.post('https://api.vapi.ai/call', json={
            'assistantId': ASSISTANT,
            'phoneNumberId': PHONE_ID,
            'customer': {'number': NUMBER},
            'metadata': {'latency_test': LABEL},
        })
        print('dial:', r.status_code)
        if r.status_code >= 300:
            print(r.text[:800]); return
        cid = r.json()['id']
        print('call id:', cid)
        for i in range(60):
            await asyncio.sleep(5)
            d = (await c.get(f'https://api.vapi.ai/call/{cid}')).json()
            st = d.get('status')
            print(f'  t+{(i+1)*5:>3}s  status={st}')
            if st == 'ended':
                break
        print()
        print('endedReason:', d.get('endedReason'))
        print('duration:', round((d.get('costBreakdown') or {}).get('transport', 0), 4), '| costs:',
              {k: v for k, v in (d.get('costBreakdown') or {}).items() if 'lm' in k.lower()})
        msgs = [m for m in (d.get('messages') or []) if m.get('role') in ('bot', 'user')]
        prev, gaps = None, []
        print()
        for m in msgs:
            sfs = m.get('secondsFromStart')
            dur = (m.get('duration') or 0) / 1000
            gap = ''
            if m.get('role') == 'bot' and prev is not None and prev.get('role') == 'user':
                g = round(sfs - ((prev.get('secondsFromStart') or 0) + (prev.get('duration') or 0) / 1000), 2)
                if 0 < g < 30:
                    gaps.append(g); gap = f'  <-- пауза {g}s'
            print(f'  {sfs:>7.2f}s  говорить {dur:>5.1f}s  {m.get("role"):<5} {(m.get("message") or "")[:80]}{gap}')
            prev = m
        print()
        first = next((m.get('secondsFromStart') for m in msgs if m.get('role') == 'bot'), None)
        print('ПЕРША ФРАЗА:', first, 's')
        print('ПАУЗИ ЄВИ:', gaps, '| медіана:', round(sorted(gaps)[len(gaps)//2], 2) if gaps else None)
        longest = max([(m.get('duration') or 0) / 1000 for m in msgs if m.get('role') == 'bot'] or [0])
        print('НАЙДОВША РЕПЛІКА ЄВИ:', round(longest, 1), 's')

asyncio.run(main())
