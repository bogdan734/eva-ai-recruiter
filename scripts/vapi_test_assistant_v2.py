"""v2 of the test assistant: kill the dead air at the start of the call.

What the 08:31 test call actually looked like to the human:

    0.4s  Eva greets -- the phone is still travelling to her ear, she misses it
    2.1s  Eva has finished; silence
    7.0s  she says "Алло." into what sounds like a dead line
    8.8s  Eva finally introduces herself

Nearly nine seconds before she hears a sentence that means anything. Endpointing
tuning cannot touch that: the cost is structural. The greeting spends a whole
model round trip -- on the coldest prefill of the call, 7.2k tokens -- to say
something the script already knows word for word. So put the introduction into
firstMessage, where it costs nothing, and let the first model turn answer a
question the candidate has actually been asked.
"""
import httpx

KEY = ''
for line in open('/app/.env'):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}
AID = '574db2a7-f6b3-4df6-a124-a44a36378fb3'

d = httpx.get(f'https://api.vapi.ai/assistant/{AID}', headers=H, timeout=30).json()
p = d['model']['messages'][0]['content']

OLD_1 = '''STEP 1 — GREETING (already spoken by the system)
   Your first line "Добрий день!" is played automatically with a built-in pause
   before it — do NOT greet again. WAIT for the candidate to FULLY finish
   greeting back ("Алло" / "Добрий день" / "Слухаю"). Do NOT start talking until
   they finish.

STEP 2 — PRESENTATION (two short turns — NEVER one long monologue)
   NEVER speak more than two sentences in a row. A long uninterrupted pitch makes
   the candidate think the line dropped — they start saying "Алло?" over you.

   TURN 1 — say ONLY this, then STOP and wait for any reply ("так", "слухаю", "ага"):
   "Мене звати Єва, я помічниця рекрутера компанії Козир Транс,
   організація вантажоперевезень. Зручно зараз говорити?"

   TURN 2 — after they respond, say the offer, then go STRAIGHT to '''

NEW_1 = '''STEP 1 — GREETING AND INTRODUCTION (already spoken by the system)
   The system has ALREADY said, automatically, before your first turn:
   "Алло? Добрий день! Мене звати Єва, я помічниця рекрутера компанії Козир
   Транс, організація вантажоперевезень. Зручно зараз говорити?"
   NEVER greet again. NEVER introduce yourself again. NEVER ask again whether it
   is convenient. The candidate has already heard all of it and is answering it.

STEP 2 — THE OFFER (ONE short turn)
   Their reply to "Зручно зараз говорити?" is what you are answering now.

   • Not convenient / busy → offer to call back, agree on a time, "Гарного дня!",
     END THE CALL.
   • Not interested / not looking → CLOSE-SCRIPT-A → END THE CALL.
   • Anything affirmative ("так", "зручно", "слухаю", "ага") → say the offer
     below, ONE sentence, then go STRAIGHT to '''

assert p.count(OLD_1) == 1, 'STEP 1/2 block not found exactly once'
p = p.replace(OLD_1, NEW_1, 1)

# The scripted pitch IS the monologue: one sentence carrying job, employment
# type, remoteness and salary runs ~14s of Ukrainian speech. Split it -- the
# rest of the facts are answers to the candidate's own questions.
OLD_2 = '''"У нас відкрита вакансія менеджера з продажу логістики бі-ту-бі: повна зайнятість,
   стовідсотково віддалено, дохід від тридцяти до шістдесяти п'яти тисяч гривень і вище."'''
NEW_2 = '''"У нас відкрита вакансія менеджера з продажу логістики, повністю віддалено."
   Say NOTHING else here -- no salary, no schedule, no benefits, no company
   story. Those are answers to questions, and the candidate has not asked yet.
   If they ask about pay, SALARY QUESTIONS below applies.'''
assert p.count(OLD_2) == 1, 'pitch sentence not found exactly once'
p = p.replace(OLD_2, NEW_2, 1)

body = {
    'firstMessage': 'Алло? Добрий день! Мене звати Єва, я помічниця рекрутера '
                    'компанії Козир Транс, організація вантажоперевезень. '
                    'Зручно зараз говорити?',
    'model': {
        'provider': 'anthropic',
        'model': d['model']['model'],
        'temperature': d['model'].get('temperature', 0.4),
        # 120 tokens of Ukrainian is still ~14 seconds of speech, so the old cap
        # never bound. 75 is roughly two spoken sentences.
        'maxTokens': 75,
        'messages': [{'role': 'system', 'content': p}],
    },
}
r = httpx.patch(f'https://api.vapi.ai/assistant/{AID}', headers=H, json=body, timeout=60)
print('patch:', r.status_code)
if r.status_code >= 300:
    print(r.text[:1500])
else:
    n = r.json()
    print('firstMessage:', n['firstMessage'])
    print('maxTokens:', n['model']['maxTokens'],
          '| prompt chars:', len(n['model']['messages'][0]['content']))
