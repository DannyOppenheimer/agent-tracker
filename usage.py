"""Quota collection without model calls or stored credentials."""
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import textwrap
import threading
import time


def codex_usage(command):
    process = subprocess.Popen(command + ['app-server'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    def response(identity):
        deadline = time.monotonic() + 20
        while True:
            message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Codex app-server exited')
            if message.get('id') == identity:
                if 'error' in message:
                    raise RuntimeError('Codex usage unavailable (sign in with ChatGPT)')
                return message['result']
            if time.monotonic() >= deadline:
                raise TimeoutError('Codex usage timed out')
    try:
        send(dict(id=1, method='initialize', params=dict(clientInfo=dict(
            name='claude_tracker', title='Claude Tracker', version='1.0'))))
        response(1)
        send(dict(method='initialized'))
        send(dict(id=2, method='account/rateLimits/read'))
        return response(2)
    finally:
        process.kill() if process.poll() is None else None
        process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()


def capture_claude(payload, home):
    """Called by Claude's status line. Store only display data, never chat text."""
    saved = {key: payload[key] for key in ('rate_limits', 'model', 'cost', 'context_window') if key in payload}
    saved['updated_at'] = time.time()
    folder = Path(home) / 'tracker-usage'
    folder.mkdir(parents=True, exist_ok=True)
    # Per-session files avoid races between concurrently active terminals.
    import hashlib
    name = hashlib.sha256(str(payload.get('session_id', 'default')).encode()).hexdigest()[:24]
    target = folder / (name + '.json')
    temporary = folder / (name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(saved), encoding='utf-8')
    temporary.replace(target)
    model = (saved.get('model') or {}).get('display_name', 'Claude')
    context = (saved.get('context_window') or {}).get('used_percentage')
    print(model + (f' | context {context:g}%' if isinstance(context, (int, float)) else ''))


def antigravity_home():
    return Path.home() / '.gemini' / 'antigravity-cli'


def capture_antigravity(payload, home=None):
    """Persist only documented display fields; no credentials or chat content."""
    saved = {key: payload[key] for key in ('quota', 'model', 'context_window', 'plan_tier', 'cost') if key in payload}
    saved['updated_at'] = time.time()
    import hashlib
    name = hashlib.sha256(str(payload.get('session_id') or payload.get('conversation_id') or 'default').encode()).hexdigest()[:24]
    folder = Path(home or antigravity_home()) / 'tracker-usage'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name + '.json')
    temp = folder / (name + '.' + str(os.getpid()) + '.tmp')
    temp.write_text(json.dumps(saved), encoding='utf-8')
    temp.replace(target)
    print('Gemini usage shared with claude-tracker')


def antigravity_snapshot(home=None):
    latest, quota = {}, {}
    for path in (Path(home or antigravity_home()) / 'tracker-usage').glob('*.json'):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(item, dict):
                continue
            if item.get('updated_at', 0) > latest.get('updated_at', 0):
                latest = item
            if item.get('quota') and item.get('updated_at', 0) > quota.get('updated_at', 0):
                quota = item
        except (OSError, ValueError, TypeError):
            continue
    if quota:
        latest = dict(latest, quota=quota['quota'], quota_updated_at=quota['updated_at'])
    return latest


def gemini_lines(snapshot, now=None):
    from datetime import datetime
    now = time.time() if now is None else now
    data = snapshot.get('Gemini') or {}
    stamp = data.get('quota_updated_at', data.get('updated_at', 0))
    lines = ['Gemini usage | updated ' + (duration(now-stamp) + ' ago' if stamp else 'never')]
    if data.get('plan_tier'):
        lines.append('  plan: ' + str(data['plan_tier']))
    quota = data.get('quota') or {}
    if not quota:
        lines.append('  Waiting for Antigravity status-line data; restart agy and send a prompt.')
    for label, bucket in quota.items():
        if not isinstance(bucket, dict):
            continue
        if bucket.get('disabled'):
            lines.append('  ' + label + ': disabled')
            continue
        reset = bucket.get('reset_time')
        if isinstance(reset, str):
            try:
                reset = datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()
            except ValueError:
                reset = None
        if reset is None and isinstance(bucket.get('reset_in_seconds'), (int, float)):
            reset = stamp + bucket['reset_in_seconds']
        fraction = bucket.get('remaining_fraction')
        if isinstance(fraction, (int, float)) and math.isfinite(fraction) and 0 <= fraction <= 1:
            lines.append('  ' + window_text(label, dict(usedPercent=round((1-fraction)*100, 2), resetsAt=reset), now))
        elif isinstance(bucket.get('remaining_amount'), (int, float)):
            lines.append(f"  {label}: {bucket['remaining_amount']:g} remaining | resets {reset_text(reset, now)}")
        else:
            lines.append('  ' + label + ': unavailable')
    return lines


def duration(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f'{days}d ' if days else '') + (f'{hours}h ' if hours or days else '') + f'{minutes}m'


def reset_text(timestamp, now):
    if not isinstance(timestamp, (int, float)):
        return 'reset unknown'
    local = time.strftime('%a %H:%M', time.localtime(timestamp))
    return ('awaiting refresh' if timestamp <= now else 'in ' + duration(timestamp - now)) + f' ({local})'


def window_text(label, window, now, claude=False):
    percent = window.get('used_percentage' if claude else 'usedPercent')
    if not isinstance(percent, (int, float)) or not math.isfinite(percent):
        return label + ': unavailable'
    filled = round(max(0, min(100, percent)) / 10)
    reset = window.get('resets_at' if claude else 'resetsAt')
    text = f'{label} [{"#" * filled}{"-" * (10-filled)}] {percent:g}% used ({max(0, 100-percent):g}% left) | resets {reset_text(reset, now)}'
    if window.get('used_usd') is not None:
        text += f' | ${window["used_usd"]:g}/${window.get("limit_usd", 0):g}'
    return text


class Usage:
    def __init__(self, home, command, start=True):
        self.home, self.command = Path(home), command
        self.lock = threading.Lock()
        self.data, self.updated, self.error = {}, 0, ''
        self.stop = threading.Event()
        if start:
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while not self.stop.is_set():
            try:
                result = codex_usage(self.command())
                with self.lock:
                    self.data, self.updated, self.error = result, time.time(), ''
            except Exception:
                with self.lock:
                    self.error = 'refresh failed; check Codex login/network'
            self.stop.wait(60)

    def snapshot(self):
        latest = {}
        latest_quota = {}
        for path in (self.home / 'tracker-usage').glob('*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record.get('updated_at', 0) > latest.get('updated_at', 0):
                    latest = record
                if record.get('rate_limits') and record.get('updated_at', 0) > latest_quota.get('updated_at', 0):
                    latest_quota = record
            except (OSError, ValueError, AttributeError):
                continue
        # A new session without quotas must not erase an older usable reading.
        if latest_quota:
            latest['rate_limits'] = latest_quota['rate_limits']
            latest['quota_updated_at'] = latest_quota['updated_at']
        with self.lock:
            return dict(Claude=latest, Codex=dict(data=self.data, updated_at=self.updated, error=self.error), Gemini=antigravity_snapshot())

    def lines(self, now=None, width=120, page=0):
        return usage_panels(self.snapshot(), width, now, page)


def usage_lines(snapshot, now=None):
    now = time.time() if now is None else now
    lines = []
    claude = snapshot.get('Claude') or {}
    limits = claude.get('rate_limits') or {}
    stamp = claude.get('quota_updated_at', claude.get('updated_at', 0))
    age = duration(now-stamp) if stamp else 'never'
    lines.append('Claude usage | updated ' + age + (' ago' if stamp else '') + ' | reset credits: not exposed')
    if not limits:
        lines.append('  Waiting for Claude status-line data; send a prompt in Claude (restart session if needed).')
    for key, window in limits.items():
        if isinstance(window, dict):
            label = {'five_hour': '5h', 'seven_day': 'week'}.get(key, key.replace('_', ' '))
            lines.append('  ' + window_text(label, window, now, True))
    context = (claude.get('context_window') or {}).get('used_percentage')
    cost = (claude.get('cost') or {}).get('total_cost_usd')
    extras = []
    if context is not None:
        extras.append(f'latest session context: {context:g}%')
    if cost is not None:
        extras.append(f'session API-equivalent cost: ${cost:.4f}')
    if extras:
        lines.append('  ' + ' | '.join(extras))
    codex = snapshot.get('Codex') or {}
    data = codex.get('data') or {}
    stamp = codex.get('updated_at', 0)
    credits = data.get('rateLimitResetCredits') or {}
    count = credits.get('availableCount')
    lines.append('Codex usage | updated ' + (duration(now-stamp) + ' ago' if stamp else 'never') +
        ' | resets left: ' + (str(count) if count is not None else 'not reported') +
        (' | ' + codex['error'] if codex.get('error') else ''))
    buckets = data.get('rateLimitsByLimitId') or {}
    if not buckets and data.get('rateLimits'):
        buckets = {'codex': data['rateLimits']}
    if not buckets:
        lines.append('  ' + ('Usage unavailable' if codex.get('error') else 'Fetching account limits...'))
    for identity, bucket in buckets.items():
        metadata = []
        if bucket.get('planType'):
            metadata.append('plan: ' + str(bucket['planType']))
        if bucket.get('spendControlReached'):
            metadata.append('spend control reached')
        if data.get('ordinaryUsageAllowed') is False:
            metadata.append('ordinary usage blocked')
        if metadata:
            lines.append('  ' + ' | '.join(metadata))
        for key in ('primary', 'secondary'):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            mins = window.get('windowDurationMins')
            label = {300: '5h', 10080: 'week'}.get(mins, duration(mins*60) if isinstance(mins, (int, float)) else key)
            prefix = (bucket.get('limitName') or identity) + ' ' if len(buckets) > 1 else ''
            lines.append('  ' + window_text(prefix + label, window, now))
        balance = bucket.get('credits') or {}
        if balance:
            lines.append('  Credits: ' + ('unlimited' if balance.get('unlimited') else str(balance.get('balance', 'not reported'))) +
                         ' | credits enabled: ' + str(balance.get('hasCredits', False)))
        if bucket.get('rateLimitReachedType'):
            lines.append('  Limit reached: ' + str(bucket['rateLimitReachedType']))
    for credit in credits.get('credits') or []:
        if credit.get('status') == 'available':
            lines.append('  ' + str(credit.get('title') or 'Reset credit') + ' | expires ' + reset_text(credit.get('expiresAt'), now))
    return lines


# True-color terminal palette: warm Claude, cool Codex, bright primary text.
ESC = '\x1b['
RESET = ESC + '0m'
WHITE = ESC + '38;2;235;241;250m'
MUTED = ESC + '38;2;158;176;198m'
TRACK = ESC + '38;2;56;72;94m'
GOLD = ESC + '38;2;255;202;104m'
RED = ESC + '38;2;255;112;125m'
MINT = ESC + '38;2;112;224;185m'
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def usage_panels(snapshot, width, now=None, page=0):
    """Compose bounded provider cards; wrap plain text before adding ANSI color."""
    raw = usage_lines(snapshot, now)
    split = next(i for i, line in enumerate(raw) if line.startswith('Codex usage'))
    providers = []
    if snapshot.get('Claude'):
        providers.append(('Claude', raw[:split], ESC+'38;2;245;164;110m'))
    if (snapshot.get('Codex') or {}).get('data'):
        providers.append(('Codex', raw[split:], ESC+'38;2;116;190;255m'))
    if (page or not providers) and snapshot.get('Gemini'):
        providers = [('Gemini', gemini_lines(snapshot, now), MINT)]
    if not providers:
        return []
    side_by_side = len(providers) == 2 and width >= 110
    card_width = max(12, (width - 5) // 2 if side_by_side else width - 2)

    def safe(text):
        return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(text))

    def card(name, source, accent):
        inside = card_width - 4
        rows = []
        def row(text='', style=WHITE):
            # Padding counts visible characters only, never escape sequences.
            visible = len(ANSI.sub('', text))
            rows.append(accent + '│ ' + style + text + ' ' * max(0, inside-visible) + RESET + accent + ' │' + RESET)
        def wrapped(text, style=MUTED):
            for part in textwrap.wrap(safe(text), width=inside) or ['']:
                row(part, style)
        title = (' ' + name.upper() + ' / USAGE ')[:card_width-3]
        rows.append(accent + '╭─' + ESC + '1m' + title + RESET + accent + '─' * max(0, card_width-len(title)-3) + '╮' + RESET)
        header = safe(source[0]).split(' | ')
        # Keep freshness secondary; resets are a distinct, bright badge.
        freshness = next((x for x in header if x.startswith('updated ')), '')
        resets = next((x for x in header if x.startswith(('resets left:', 'reset credits:'))), '')
        if name == 'Codex':
            wrapped(resets.upper(), MINT + ESC + '1m')
        for item in source[1:]:
            text = safe(item).strip()
            match = re.match(r'(.+?) \[[#-]+\] ([\d.]+)% used \(([\d.]+)% left\) \| resets (.*)', text)
            if match:
                label, percent, remaining, reset = match.groups()
                value = float(percent)
                ink = RED if value >= 90 else GOLD if value >= 70 else accent
                # Solid fill with a fractional end-cap; the percentage remains exact.
                cells = max(4, min(18, inside - len(label) - len(percent) - 12))
                units = round(max(0, min(100, value)) / 100 * cells * 8)
                full, fraction = divmod(units, 8)
                bar = '█' * full + ('▏▎▍▌▋▊▉'[fraction-1] if fraction else '')
                empty = cells - full - bool(fraction)
                meter = ink + bar + TRACK + '░' * empty + RESET
                summary = f'{label.upper():<5} ' + meter + ink + ESC + '1m' + f' {percent}% used' + RESET
                if len(ANSI.sub('', summary)) <= inside:
                    row(summary)
                else:
                    wrapped(f'{label.upper()} {percent}% used', ink)
                wrapped(f'↳ {remaining}% left · reset {reset}', WHITE)
            elif text.startswith('Full reset') or ' | expires ' in text:
                wrapped('↻ ' + text.replace(' | expires ', ' · expires '), MINT)
            elif text.startswith('Limit reached:') or 'blocked' in text or 'reached' in text:
                wrapped('! ' + text, RED)
            elif text.startswith('plan:'):
                wrapped(text.replace('plan:', 'PLAN').replace(' | ', ' · '), accent)
            else:
                text = text.replace('latest session context:', 'Context').replace(
                    'session API-equivalent cost:', 'Session ≈').replace('credits enabled: False', 'no paid credits')
                wrapped(text.replace(' | ', ' · '), MUTED)
        row('─' * inside, TRACK)
        wrapped('◷ ' + freshness, MUTED)
        if name == 'Claude':
            wrapped('Reset credits unavailable', MUTED)
        error = (snapshot.get(name) or {}).get('error')
        if error:
            wrapped('! ' + error, GOLD)
        rows.append(accent + '╰' + '─' * (card_width-2) + '╯' + RESET)
        return rows

    cards = [card(*provider) for provider in providers]
    if not side_by_side:
        return [' ' + line for rows in cards for line in rows]
    height = max(map(len, cards))
    for index, (_, _, accent) in enumerate(providers):
        # Equal-height silhouettes make the two panels read as one dashboard.
        while len(cards[index]) < height:
            cards[index].insert(-1, accent + '│' + ' ' * (card_width-2) + '│' + RESET)
    result = []
    for i in range(height):
        left = cards[0][i] if i < len(cards[0]) else ' ' * card_width
        right = cards[1][i] if i < len(cards[1]) else ' ' * card_width
        result.append(' ' + left + '   ' + right)
    return result
