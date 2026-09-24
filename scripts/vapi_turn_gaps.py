"""Measure the silence the candidate actually sits through.

For every bot turn that follows a user turn, gap = bot.secondsFromStart minus
(user.secondsFromStart + user.duration/1000). That is the whole round trip the
human perceives: endpointing + LLM + TTS + network. It is the number to move;
any single component (prompt caching included) only ever shaves part of it.
"""
import asyncio, json, os, statistics, sys
import httpx

KEY = ''
for line in open(os.environ.get('ENV_FILE', '/app/.env')):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()

IDS = sys.argv[1:]


async def main():
    gaps, firsts, rows = [], [], []
    async with httpx.AsyncClient(timeout=30, headers={'Authorization': f'Bearer {KEY}'}) as c:
        for cid in IDS:
            r = await c.get(f'https://api.vapi.ai/call/{cid}')
            if r.status_code != 200:
                continue
            d = r.json()
            msgs = [m for m in (d.get('messages') or []) if m.get('role') in ('bot', 'user')]
            if not msgs:
                continue
            cb = d.get('costBreakdown') or {}
            prev = None
            local = []
            for m in msgs:
                if m.get('role') == 'bot' and prev is not None and prev.get('role') == 'user':
                    end_user = (prev.get('secondsFromStart') or 0) + (prev.get('duration') or 0) / 1000
                    g = round((m.get('secondsFromStart') or 0) - end_user, 2)
                    if 0 < g < 30:
                        gaps.append(g); local.append(g)
                prev = m
            first_bot = next((m for m in msgs if m.get('role') == 'bot'), None)
            if first_bot:
                firsts.append(round(first_bot.get('secondsFromStart') or 0, 2))
            rows.append((cid[:8], d.get('endedReason'), len(msgs),
                         cb.get('llmPromptTokens'), cb.get('llmCachedPromptTokens'),
                         local))
    print(f'{"call":<10}{"endedReason":<42}{"msgs":>5}{"prompt":>8}{"cached":>8}  gaps(s)')
    for r in rows:
        print(f'{r[0]:<10}{str(r[1])[:40]:<42}{r[2]:>5}{str(r[3]):>8}{str(r[4]):>8}  {r[5]}')
    print()
    if gaps:
        gaps.sort()
        print(f'ВІДПОВІДЬ ЄВИ ПІСЛЯ РЕПЛІКИ КАНДИДАТА  (n={len(gaps)})')
        print(f'  мін    {gaps[0]:.2f} s')
        print(f'  медіана{statistics.median(gaps):.2f} s')
        print(f'  серед. {statistics.mean(gaps):.2f} s')
        print(f'  p90    {gaps[int(len(gaps)*0.9)-1]:.2f} s')
        print(f'  макс   {gaps[-1]:.2f} s')
    if firsts:
        firsts.sort()
        print(f'\nПЕРША ФРАЗА ЄВИ ПІСЛЯ ПІДНЯТТЯ СЛУХАВКИ  (n={len(firsts)})')
        print(f'  медіана{statistics.median(firsts):.2f} s   мін {firsts[0]:.2f}   макс {firsts[-1]:.2f}')

asyncio.run(main())
