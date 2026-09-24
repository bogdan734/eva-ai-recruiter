"""How big is the prompt we actually send, in characters and in real tokens?

Prompt caching has a minimum cacheable prefix per model; below it Anthropic
silently ignores cache_control. So before wiring caching up, measure -- a prompt
under the floor cannot be cached no matter how the request is shaped.
"""
from src.call.script_template import render_system_prompt

p = render_system_prompt(
    candidate_name='Тест Тестенко',
    candidate_phone='+380670000000',
    candidate_position='Менеджер з продажу',
    source='workua_response_send',
    company_pitch=None, vacancy_schedule=None, vacancy_benefits=None,
    vacancy_title='Менеджер з продажу логістики бі-ту-бі',
    vacancy_pitch='', vacancy_requirements='',
    vacancy_salary='від тридцяти до шістдесяти п\'яти тисяч гривень',
    vacancy_location='Україна',
)
print('chars:', len(p))
print('words:', len(p.split()))
try:
    import anthropic, os
    key = ''
    for line in open(os.environ.get('ENV_FILE', '/app/.env')):
        if line.startswith('ANTHROPIC_API_KEY='):
            key = line.split('=', 1)[1].strip()
    c = anthropic.Anthropic(api_key=key)
    r = c.messages.count_tokens(
        model='claude-haiku-4-5-20251001',
        system=p,
        messages=[{'role': 'user', 'content': 'Алло'}],
    )
    print('REAL tokens (count_tokens):', r.input_tokens)
except Exception as e:
    print('count_tokens unavailable:', type(e).__name__, e)
print()
print('--- first 600 chars ---')
print(p[:600])
