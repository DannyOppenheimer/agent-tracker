"""Translate agent payloads into the shared tile contract.

Add a ProviderSpec to PROVIDERS for a new source. Sources already returning the
normalized allowance schema can reuse allowance_adapter without a new renderer.
"""
from dataclasses import dataclass
from typing import Callable

from quota import timestamp
from usage_tiles import Billing, Status, UsageTile, UsageWindow, TokenSpend, TileFact, finite, duration


def mapping(value):
    return value if isinstance(value, dict) else {}


def sequence(value):
    return value if isinstance(value, (list, tuple)) else ()


def numeric(value):
    return value if finite(value) and value >= 0 else None


def text(value):
    return value if isinstance(value, str) else ''


def percent(value):
    # Values above 100 can represent a provider-reported overage. The renderer
    # caps only the bar, keeping the reported percentage visible.
    return numeric(value)


def billing(value):
    try:
        return Billing(value)
    except (ValueError, TypeError):
        return Billing.UNKNOWN


def token_spend(value):
    value = mapping(value)
    tokens, usd = numeric(value.get('tokens')), numeric(value.get('cost_usd'))
    if tokens is None and usd is None:
        return None
    return TokenSpend(tokens=tokens, cost_usd=usd, estimated=value.get('estimated') is True,
                      scope=text(value.get('scope')) or 'Reported usage')


def make_tile(spec, state, data, *, windows=(), facts=(), plan='', updated_at=None,
              message='', ready=False, default_billing=Billing.UNKNOWN):
    mode = billing(data.get('billing_mode') or state.get('billing_mode'))
    if mode == Billing.UNKNOWN:
        mode = default_billing
    spend = token_spend(data.get('token_spend')) if mode == Billing.TOKEN else None
    if data.get('subscription_status') == 'inactive' and mode != Billing.TOKEN:
        status, windows = Status.NO_SUBSCRIPTION, ()
    elif ready or mode == Billing.TOKEN or any(w.used_percent is not None for w in windows):
        status = Status.READY
    elif state.get('error') or data:
        status = Status.UNAVAILABLE
    else:
        status = Status.LOADING
    return UsageTile(provider=spec.name, accent=spec.accent, billing=mode, status=status,
        windows=tuple(windows), facts=tuple(facts), spend=spend, plan=plan,
        updated_at=numeric(updated_at), error=text(state.get('error')),
        message=message if status not in (Status.READY, Status.NO_SUBSCRIPTION) else '')


def allowance_adapter(spec, state):
    """Common allowance schema used by Muse, Grok, and future integrations.

    billing_mode: subscription|token|unknown; subscription_status: inactive
    windows: [{label, usedPercent, resetsAt}]
    token_spend: {tokens, cost_usd, estimated, scope} (token billing only)
    details: [{label, value, tone, expires_at}] (optional supplementary facts)
    """
    state = mapping(state)
    data = mapping(state.get('data'))
    windows = [UsageWindow(text(w.get('label')) or 'allowance', percent(w.get('usedPercent')),
                           timestamp(w.get('resetsAt')), w.get('disabled') is True)
               for w in sequence(data.get('windows')) if isinstance(w, dict)]
    facts = []
    for fact in sequence(data.get('details')):
        if isinstance(fact, dict):
            facts.append(TileFact(text(fact.get('label')), text(fact.get('value')),
                                  text(fact.get('tone')) or 'muted', timestamp(fact.get('expires_at'))))
    # Compatibility with earlier collector snapshots; prose is never parsed.
    if 'details' not in data:
        facts = [TileFact('', note) for note in sequence(data.get('notes')) if isinstance(note, str)]
    return make_tile(spec, state, data, windows=windows, facts=facts, plan=text(data.get('plan')),
        updated_at=data.get('observed_at') or state.get('updated_at'),
        default_billing=Billing.SUBSCRIPTION if windows else Billing.UNKNOWN)


def claude_adapter(spec, state):
    state = mapping(state)
    limits = mapping(state.get('rate_limits'))
    windows = [UsageWindow({'five_hour': '5h', 'seven_day': 'week'}.get(key, key.replace('_', ' ')),
                           percent(w.get('used_percentage')), timestamp(w.get('resets_at')))
               for key, w in limits.items() if isinstance(w, dict)]
    data = dict(state)
    if billing(state.get('billing_mode')) == Billing.TOKEN and not state.get('token_spend'):
        # These are the latest session's reported counters, never account totals.
        context = mapping(state.get('context_window'))
        inp, out = numeric(context.get('session_input_tokens')), numeric(context.get('session_output_tokens'))
        data['token_spend'] = dict(tokens=inp+out if inp is not None and out is not None else None,
                                  cost_usd=mapping(state.get('cost')).get('total_cost_usd'),
                                  estimated=True, scope='Latest session · API-equivalent estimate')
    return make_tile(spec, state, data, windows=windows,
        updated_at=state.get('quota_updated_at', state.get('updated_at')),
        message='' if windows else 'Usage unavailable; waiting for Claude status-line data.',
        default_billing=Billing.SUBSCRIPTION if windows else Billing.UNKNOWN)


def codex_adapter(spec, state):
    state = mapping(state)
    data = mapping(state.get('data'))
    buckets = mapping(data.get('rateLimitsByLimitId'))
    if not buckets and isinstance(data.get('rateLimits'), dict):
        buckets = {'codex': data['rateLimits']}
    windows, facts, plans = [], [], []
    credits = mapping(data.get('rateLimitResetCredits'))
    count = numeric(credits.get('availableCount'))
    if count is not None:
        facts.append(TileFact('resets left', f'{count:g}', 'highlight'))
    for identity, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        plan = text(bucket.get('planType'))
        if plan and plan not in plans:
            plans.append(plan)
        for key in ('primary', 'secondary'):
            w = bucket.get(key)
            if not isinstance(w, dict):
                continue
            minutes = numeric(w.get('windowDurationMins'))
            label = {300: '5h', 10080: 'week'}.get(minutes, duration(minutes*60) if minutes else key)
            if len(buckets) > 1:
                label = (text(bucket.get('limitName')) or str(identity)) + ' ' + label
            windows.append(UsageWindow(label, percent(w.get('usedPercent')), timestamp(w.get('resetsAt'))))
        balance = mapping(bucket.get('credits'))
        if balance:
            value = 'unlimited' if balance.get('unlimited') else str(balance.get('balance', 'not reported'))
            facts.append(TileFact('Credits', value))
        for key, label in [('spendControlReached', 'Spend control reached'),
                           ('rateLimitReachedType', 'Limit reached')]:
            if bucket.get(key):
                facts.append(TileFact(label, str(bucket[key]), 'danger'))
    if data.get('ordinaryUsageAllowed') is False:
        facts.append(TileFact('Usage', 'ordinary usage blocked', 'danger'))
    for credit in sequence(credits.get('credits')):
        if isinstance(credit, dict) and credit.get('status') == 'available':
            facts.append(TileFact('', text(credit.get('title')) or 'Reset credit', 'highlight',
                                  timestamp(credit.get('expiresAt'))))
    return make_tile(spec, state, data, windows=windows, facts=facts, plan=' / '.join(plans),
        updated_at=state.get('updated_at'), ready=bool(facts),
        default_billing=Billing.SUBSCRIPTION if windows else Billing.UNKNOWN)


def gemini_adapter(spec, state):
    state = mapping(state)
    stamp = numeric(state.get('quota_updated_at', state.get('updated_at')))
    windows, facts = [], []
    for label, w in mapping(state.get('quota')).items():
        if not isinstance(w, dict):
            continue
        reset = timestamp(w.get('reset_time'))
        if reset is None and numeric(w.get('reset_in_seconds')) is not None and stamp is not None:
            reset = stamp + w['reset_in_seconds']
        fraction = numeric(w.get('remaining_fraction'))
        if w.get('disabled'):
            windows.append(UsageWindow(label, disabled=True))
        elif fraction is not None and fraction <= 1:
            windows.append(UsageWindow(label, round((1-fraction)*100, 2), reset))
        elif numeric(w.get('remaining_amount')) is not None:
            facts.append(TileFact(label, f"{w['remaining_amount']:g} remaining", expires_at=reset))
        else:
            windows.append(UsageWindow(label, resets_at=reset))
    return make_tile(spec, state, state, windows=windows, facts=facts, plan=text(state.get('plan_tier')),
        updated_at=stamp, ready=bool(facts), default_billing=Billing.SUBSCRIPTION if windows else Billing.UNKNOWN)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    accent: str
    adapter: Callable = allowance_adapter
    visible: Callable = lambda state: bool(mapping(state).get('detected') or mapping(state).get('data'))


PROVIDERS = (
    ProviderSpec('Claude', '\x1b[38;2;245;164;110m', claude_adapter, bool),
    ProviderSpec('Codex', '\x1b[38;2;116;190;255m', codex_adapter),
    ProviderSpec('Gemini', '\x1b[38;2;112;224;185m', gemini_adapter, bool),
    ProviderSpec('Muse', '\x1b[38;2;255;153;204m'),
    ProviderSpec('Grok', '\x1b[38;2;170;170;170m'),
)


def tile_pages(snapshot, providers=PROVIDERS):
    tiles = []
    for spec in providers:
        state = snapshot.get(spec.name) or {}
        if spec.visible(state):
            try:
                tile = spec.adapter(spec, state)
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                tile = UsageTile(spec.name, spec.accent, status=Status.UNAVAILABLE,
                                 error='Usage response format not recognized')
            tiles.append(tile)
    tiles.sort(key=lambda tile: tile.provider.casefold())
    return [tiles[start:start+2] for start in range(0, len(tiles), 2)]
