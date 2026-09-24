"""Apply the measured call-opening config to the LIVE assistant.

Writes the previous values to state/vapi_assistant_rollback_20260923.json first.
Rolling back is then a matter of PATCHing that file's contents straight back --
no reconstruction from memory, no guessing which number used to be what.

The system prompt on the assistant is updated for consistency, but note it is
not what a real call uses: orchestrator.py overrides model.messages per
candidate. That half ships in the code.
"""
import json
import pathlib
import httpx

KEY = AID = ''
for line in open('/app/.env'):
    if line.startswith('VAPI_API_KEY='):
        KEY = line.split('=', 1)[1].strip()
    if line.startswith('VAPI_ASSISTANT_ID='):
        AID = line.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}

live = httpx.get(f'https://api.vapi.ai/assistant/{AID}', headers=H, timeout=30).json()

ROLLBACK_KEYS = ('firstMessage', 'firstMessageMode', 'responseDelaySeconds',
                 'llmRequestDelaySeconds', 'startSpeakingPlan', 'transcriber')
rollback = {k: live.get(k) for k in ROLLBACK_KEYS}
rollback['model'] = {k: v for k, v in (live.get('model') or {}).items() if k != 'messages'}
rollback['_system_prompt_chars'] = len((live['model']['messages'][0].get('content') or ''))
out = pathlib.Path('/app/state/vapi_assistant_rollback_20260923.json')
out.write_text(json.dumps(rollback, ensure_ascii=False, indent=2))
print('rollback written ->', out)
print(json.dumps(rollback, ensure_ascii=False)[:600])
print()

# The assistant-level prompt gets the same STEP 1/2 rewrite the template got, so
# the two do not drift. A call that somehow runs without overrides still says
# the right thing.
p = live['model']['messages'][0]['content']
OLD = '''STEP 1 — GREETING (already spoken by the system)
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

   TURN 2 — after they respond, say the offer, then go STRAIGHT to STEP 3:
   "У нас відкрита вакансія менеджера з продажу логістики бі-ту-бі: повна зайнятість,
   стовідсотково віддалено, дохід від тридцяти до шістдесяти п'яти тисяч гривень і вище."'''

NEW = '''STEP 1 — GREETING AND INTRODUCTION (already spoken by the system)
   The system has ALREADY said, automatically, before your first turn:
   "Алло? Добрий день! Мене звати Єва, я помічниця рекрутера компанії Козир
   Транс, організація вантажоперевезень. Зручно зараз говорити?"
   NEVER greet again. NEVER introduce yourself again. NEVER ask again whether it
   is convenient. The candidate has already heard all of it, and whatever they
   say next is their ANSWER to it.

STEP 2 — THE OFFER (ONE short turn)
   • Not convenient / busy → offer to call back, agree on a time, "Гарного дня!",
     END THE CALL.
   • Not interested / not looking → CLOSE-SCRIPT-A → END THE CALL.
   • Anything affirmative ("так", "зручно", "слухаю", "ага") → say ONLY this,
     then go STRAIGHT to STEP 3:
     "У нас відкрита вакансія менеджера з продажу логістики, повністю віддалено."

   Say NOTHING else here — no salary, no schedule, no benefits, no company
   story. Those are answers to questions the candidate has not asked yet; if
   they ask about pay, SALARY QUESTIONS below applies.'''

if p.count(OLD) == 1:
    p = p.replace(OLD, NEW, 1)
    print('assistant prompt: STEP 1/2 rewritten')
else:
    print('assistant prompt: block not found, left as is (code override is what matters)')

RX = (r"^\s*(алло|ало)?[\s,]*"
      r"(так|ні|ага|угу|добре|гаразд|зручно|незручно|слухаю|звичайно|можна|ок|окей|ok|okay)"
      r"[\s,.!?]*$")

body = {
    'firstMessage': 'Алло? Добрий день! Мене звати Єва, я помічниця рекрутера '
                    'компанії Козир Транс, організація вантажоперевезень. '
                    'Зручно зараз говорити?',
    'firstMessageMode': 'assistant-speaks-first',
    'responseDelaySeconds': 0,
    'llmRequestDelaySeconds': 0.02,
    'transcriber': {'model': 'nova-2', 'language': 'uk', 'provider': 'deepgram',
                    'endpointing': 80, 'smartFormat': True},
    'startSpeakingPlan': {
        'waitSeconds': 0.05,
        'transcriptionEndpointingPlan': {
            'onPunctuationSeconds': 0.1,
            'onNoPunctuationSeconds': 0.4,
            'onNumberSeconds': 0.35,
        },
        'customEndpointingRules': [
            {'type': 'customer', 'regex': RX, 'timeoutSeconds': 0.1},
        ],
    },
    'model': {
        'provider': 'anthropic',
        'model': live['model']['model'],
        'temperature': live['model'].get('temperature', 0.4),
        'maxTokens': 75,
        'messages': [{'role': 'system', 'content': p}],
    },
}

r = httpx.patch(f'https://api.vapi.ai/assistant/{AID}', headers=H, json=body, timeout=60)
print('PATCH live assistant:', r.status_code)
if r.status_code >= 300:
    print(r.text[:1500])
    raise SystemExit(1)
n = r.json()
print('name:', n['name'])
print('firstMessage:', n['firstMessage'])
print('maxTokens:', n['model']['maxTokens'])
print('responseDelaySeconds:', n.get('responseDelaySeconds'))
print('transcriber.endpointing:', (n.get('transcriber') or {}).get('endpointing'))
print('startSpeakingPlan:', json.dumps(n.get('startSpeakingPlan'), ensure_ascii=False))
