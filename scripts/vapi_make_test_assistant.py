"""Clone the live assistant into a TEST one with the latency diff applied.

Config only. The behavioural script is copied verbatim apart from one appended
rule about reply length -- the 15.6-second monologue that lost a candidate on
22.09 is a length problem, and length is the one thing worth changing in the
prompt without a full rewrite. Shortening the 7.2k-token script itself is a
separate, reviewable change: it alters what Eva says, and three test calls
cannot validate that.
"""
import json, os, sys
import httpx

KEY = AID = ''
for l in open('/app/.env'):
    if l.startswith('VAPI_API_KEY='):
        KEY = l.split('=', 1)[1].strip()
    if l.startswith('VAPI_ASSISTANT_ID='):
        AID = l.split('=', 1)[1].strip()
H = {'Authorization': f'Bearer {KEY}'}

SHORT_REPLY_RULE = '''

REPLY LENGTH (hard, overrides any example above):
- Maximum TWO sentences per turn, then STOP and ask ONE question.
- NEVER deliver salary + schedule + benefits + company in one turn. Split them:
  one fact, one question, wait for the answer.
- If you are about to speak for more than ~8 seconds, cut it and ask instead.
'''

live = httpx.get(f'https://api.vapi.ai/assistant/{AID}', headers=H, timeout=30).json()

body = {
    'name': 'Єва — TEST latency (копія 23.09)',
    'firstMessage': live['firstMessage'],
    'firstMessageMode': live['firstMessageMode'],
    'endCallMessage': live.get('endCallMessage'),
    'endCallPhrases': live.get('endCallPhrases'),
    'endCallFunctionEnabled': live.get('endCallFunctionEnabled'),
    'maxDurationSeconds': live.get('maxDurationSeconds'),
    'silenceTimeoutSeconds': live.get('silenceTimeoutSeconds'),
    'customerJoinTimeoutSeconds': live.get('customerJoinTimeoutSeconds'),
    'numWordsToInterruptAssistant': live.get('numWordsToInterruptAssistant'),
    'backgroundSound': live.get('backgroundSound'),
    'backchannelingEnabled': live.get('backchannelingEnabled'),
    'modelOutputInMessagesEnabled': live.get('modelOutputInMessagesEnabled'),
    'recordingEnabled': True,
    'voice': live['voice'],
    'transcriber': live['transcriber'],
    'voicemailDetection': live.get('voicemailDetection'),
    'messagePlan': live.get('messagePlan'),
    'stopSpeakingPlan': live.get('stopSpeakingPlan'),
    'backgroundSpeechDenoisingPlan': live.get('backgroundSpeechDenoisingPlan'),
    'artifactPlan': live.get('artifactPlan'),

    # ---- THE DIFF -------------------------------------------------------
    'model': {
        'provider': 'anthropic',
        'model': live['model']['model'],
        'temperature': live['model'].get('temperature', 0.4),
        'maxTokens': 120,                      # was 200 -> caps the monologue
        'messages': [{
            'role': 'system',
            'content': live['model']['messages'][0]['content'] + SHORT_REPLY_RULE,
        }],
    },
    'responseDelaySeconds': 0,                 # was 0.1
    'llmRequestDelaySeconds': 0.02,
    'startSpeakingPlan': {
        'waitSeconds': 0.1,                    # was 0.2
        'transcriptionEndpointingPlan': {
            'onPunctuationSeconds': 0.1,       # was 0.25 (Vapi default)
            'onNoPunctuationSeconds': 0.5,     # was 0.8
            'onNumberSeconds': 0.4,            # was 0.5
        },
        # Short confirmations are the turns where 0.5s of waiting is pure loss:
        # the candidate has plainly finished after "так"/"ні"/"зручно".
        'customEndpointingRules': [{
            'type': 'customer',
            'regex': r'^\s*(так|ні|ага|угу|добре|зручно|незручно|слухаю|алло|ало|звичайно|можна|okay|ok)\s*[.!?]?\s*$',
            'timeoutSeconds': 0.15,
        }],
    },
}
body = {k: v for k, v in body.items() if v is not None}

r = httpx.post('https://api.vapi.ai/assistant', headers=H, json=body, timeout=60)
print('create:', r.status_code)
if r.status_code >= 300:
    print(r.text[:2000]); sys.exit(1)
d = r.json()
print('TEST assistant id:', d['id'])
print('name:', d['name'])
print('maxTokens:', d['model']['maxTokens'], '| prompt chars:', len(d['model']['messages'][0]['content']))
print('startSpeakingPlan:', json.dumps(d.get('startSpeakingPlan'), ensure_ascii=False))
print('responseDelaySeconds:', d.get('responseDelaySeconds'))
