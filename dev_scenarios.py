"""Synthetic dashboard setups. Excluded from release builds."""
import time

from tracker import sort_sessions
from usage import demo_usage


SCENARIOS = {
    1: 'Only Codex and Muse installed; two active sessions',
    2: 'Only Codex and Muse installed; no active sessions',
    3: 'All five providers installed; ten active sessions and three usage pages',
    4: 'Only Muse installed; one session and no subscription allowance',
    5: 'Codex and Muse installed; loading and failed account reads',
    6: 'Codex and Muse installed; exhausted limits and stale account data',
    7: 'All five providers installed; thirty sessions with long task titles',
    8: 'No providers installed; empty dashboard',
}


def scenario(number, now=None):
    now = time.time() if now is None else now
    title = SCENARIOS[number]
    samples = demo_usage(now)
    samples['Muse'] = dict(detected=True, updated_at=now, data=dict(
        plan='Sample subscription', billing_mode='subscription', windows=[
            dict(label='week', usedPercent=42, resetsAt=now+86400)]))
    providers = ['Codex', 'Muse']
    snapshot = {name: samples[name] for name in providers}
    count = 2
    if number == 2:
        count = 0
    elif number in (3, 7):
        providers = ['Claude', 'Codex', 'Antigravity', 'Muse', 'Grok']
        snapshot = samples
        count = 10 if number == 3 else 30
    elif number == 4:
        providers, count = ['Muse'], 1
        snapshot = {'Muse': dict(detected=True, updated_at=now, data=dict(
            plan='Sample API-key login', subscription_status='inactive', windows=[]))}
    elif number == 5:
        snapshot = {'Codex': dict(detected=True, data={}, updated_at=0),
                    'Muse': dict(detected=True, data={}, updated_at=0,
                                 error='Simulated refresh failure; check CLI login')}
    elif number == 6:
        snapshot['Codex']['data']['rateLimits']['primary']['usedPercent'] = 100
        snapshot['Muse']['updated_at'] = now-7200
        snapshot['Muse']['error'] = 'Simulated refresh failure; showing previous reading'
        snapshot['Muse']['data']['windows'][0]['usedPercent'] = 97
    elif number == 8:
        providers, snapshot, count = [], {}, 0

    states = ['reading', 'editing', 'running', 'thinking', 'waiting']
    tasks = ['Investigate authentication flow', 'Add account settings panel',
             'Run regression suite', 'Plan database migration', 'Waiting for next prompt']
    sessions = []
    for index in range(count):
        provider = providers[index % len(providers)]
        metrics = dict(context_percent=25+index*2, total_tokens=12000+index*500)
        if provider != 'Codex':
            metrics.update(cost_usd=.0123*(index+1), cost_partial=provider == 'Muse')
        sessions.append(dict(provider=provider, name=f'{provider} sample {index+1}',
            pid=None, session=f'synthetic-{number}-{index+1:03}',
            directory=rf'C:\Sample Projects\project-{index+1}',
            task=tasks[index % len(tasks)] + (' with a deliberately long description to inspect wrapping in a narrow terminal' if number == 7 else ''),
            activity=states[index % len(states)], detail='', usage=metrics,
            last_prompt_at=now-index*60))
    sort_sessions(sessions)
    return dict(sessions=sessions, usage=snapshot,
                notice=f'TEST {number}: {title} | FAKE DATA | terminal navigation disabled')
