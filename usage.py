"""Quota collection without model calls or stored credentials."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from quota import cli_command, muse_usage, grok_usage
from session_usage import collected_sessions, session_key
from usage_adapters import PROVIDERS, tile_pages
from usage_tiles import ANSI, RED, tile_lines, render_tiles


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
    saved = {key: payload[key] for key in ('rate_limits', 'model', 'cost', 'context_window', 'billing_mode', 'token_spend') if key in payload}
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
    saved = {key: payload[key] for key in ('quota', 'model', 'context_window', 'plan_tier', 'cost', 'billing_mode', 'token_spend') if key in payload}
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


class Usage:
    def __init__(self, home, command, start=True):
        self.home, self.command = Path(home), command
        self.lock = threading.Lock()
        self.data, self.updated, self.error = {}, 0, ''
        self.stop = threading.Event()
        muse_home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'muse'
        grok_home = Path(os.environ.get('GROK_HOME') or Path.home() / '.grok')
        self.extra = {name: dict(data={}, updated_at=0, error='',
                                detected=bool(cli_command(name.lower()) or folder.is_dir()))
                      for name, folder in [('Muse', muse_home), ('Grok', grok_home)]}
        if start:
            for provider in ('Codex', 'Muse', 'Grok'):
                if provider == 'Codex' or self.extra[provider]['detected']:
                    threading.Thread(target=self._worker, args=(provider,), daemon=True).start()

    def refresh(self, provider):
        try:
            if provider == 'Codex':
                result = codex_usage(self.command())
            else:
                command = cli_command(provider.lower())
                if not command:
                    if not self.extra[provider]['detected']:
                        return
                    raise FileNotFoundError(provider + ' CLI unavailable')
                result = {'Muse': muse_usage, 'Grok': grok_usage}[provider](command)
            if not isinstance(result, dict):
                raise ValueError('Invalid usage response')
            with self.lock:
                if provider == 'Codex':
                    self.data, self.updated, self.error = result, time.time(), ''
                else:
                    self.extra[provider].update(data=result, updated_at=time.time(), error='', detected=True)
        except Exception:
            with self.lock:
                error = ('refresh failed; check ' + provider + ' CLI, login, version, and network')
                if provider == 'Codex':
                    self.error = error
                else:
                    self.extra[provider]['error'] = error

    def refresh_all(self):
        workers = [threading.Thread(target=self.refresh, args=(provider,))
                   for provider in ('Codex', 'Muse', 'Grok')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    def _worker(self, provider='Codex'):
        while not self.stop.is_set():
            self.refresh(provider)
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
            return dict(Claude=latest, Codex=dict(data=self.data, updated_at=self.updated, error=self.error),
                        Gemini=antigravity_snapshot(),
                        **{name: dict(value) for name, value in self.extra.items()})

    def observe_sessions(self, sessions, collector_homes=None):
        homes = collector_homes if collector_homes is not None else {
            'Claude': self.home, 'Antigravity': antigravity_home(),
            'Grok': Path(os.environ.get('GROK_HOME') or Path.home() / '.grok')}
        collectors = {provider: collected_sessions(home) for provider, home in homes.items()}
        for session in sessions:
            sid = session.get('session')
            if not sid:
                continue
            provider = session.get('provider', 'Claude')
            key = session_key(sid)
            metrics = collectors.get(provider, {}).get(key, session.get('usage') or {})
            session['usage'] = metrics

    def lines(self, now=None, width=120, page=0):
        return usage_panels(self.snapshot(), width, now, page)


def provider_lines(name, state, now=None):
    spec = next(spec for spec in PROVIDERS if spec.name == name)
    return tile_lines(spec.adapter(spec, state), now)


def usage_lines(snapshot, now=None):
    # Legacy plain-text entry point uses the same model as graphical tiles.
    return [line for name in ('Claude', 'Codex')
            for line in provider_lines(name, snapshot.get(name) or {}, now)]


def gemini_lines(snapshot, now=None):
    return provider_lines('Gemini', snapshot.get('Gemini') or {}, now)


def allowance_lines(name, state, now=None):
    return provider_lines(name, state, now)


def demo_usage(now=None):
    now = time.time() if now is None else now
    snapshot = dict(
        Claude=dict(updated_at=now, rate_limits=dict(five_hour=dict(used_percentage=35, resets_at=now+7200))),
        Codex=dict(updated_at=now, data=dict(rateLimits=dict(primary=dict(
            usedPercent=20, windowDurationMins=300, resetsAt=now+10800)))),
        Gemini=dict(updated_at=now, quota=dict(weekly=dict(remaining_fraction=.6, reset_in_seconds=86400))))
    # Illustrate all three display modes; these are sample billing states.
    snapshot['Muse'] = dict(detected=True, updated_at=now, data=dict(
        plan='Demo', subscription_status='inactive', windows=[]))
    snapshot['Grok'] = dict(detected=True, updated_at=now, data=dict(
        plan='Demo token billing', billing_mode='token', token_spend=dict(
            tokens=12500, cost_usd=.034, estimated=True, scope='Demo session'),
        details=[dict(label='Prepaid credits', value='$12.50')]))
    return snapshot


def usage_pages(snapshot):
    return [[tile.provider for tile in page] for page in tile_pages(snapshot)]


def usage_panels(snapshot, width, now=None, page=0):
    pages = tile_pages(snapshot)
    if not pages:
        return []
    return render_tiles(pages[min(max(0, page), len(pages)-1)], width, now,
                        sizing_tiles=[tile for group in pages for tile in group])
