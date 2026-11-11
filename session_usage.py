"""Session context, token counts, and costs reported directly by providers."""
import hashlib
import json
import math
import queue
import subprocess
import threading
from pathlib import Path
import time


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def session_key(identity):
    return hashlib.sha256(str(identity).encode()).hexdigest()[:24]


def collector_metrics(payload):
    result = {}
    context = payload.get('context_window') or {}
    percent = context.get('used_percentage')
    if number(percent) and percent <= 100:
        result['context_percent'] = percent
    cost = (payload.get('cost') or {}).get('total_cost_usd')
    if number(cost):
        result['cost_usd'] = cost
    inp, out = context.get('session_input_tokens'), context.get('session_output_tokens')
    if number(inp) and number(out):
        result['total_tokens'] = inp + out
    if number(payload.get('updated_at')):
        result['updated_at'] = payload['updated_at']
    return result


def collected_sessions(home):
    result = {}
    for path in (Path(home) / 'tracker-usage').glob('*.json'):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(payload, dict):
                result[path.stem] = collector_metrics(payload)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


def muse_snapshot_metrics(result):
    snapshot = (result.get('history') or {}).get('snapshot') or {}
    state = snapshot.get('state') or {}
    tokens = state.get('tokenUsage') or {}
    metrics = {}
    if number(tokens.get('totalTokens')):
        metrics['total_tokens'] = tokens['totalTokens']
    cost = tokens.get('cost') or {}
    if number(cost.get('usd')):
        metrics.update(cost_usd=cost['usd'], cost_partial=bool(cost.get('partial')))
    context = state.get('contextUsage') or {}
    used, window = context.get('usedTokens'), context.get('windowTokens')
    if number(used) and number(window) and window > 0:
        metrics['context_percent'] = min(100, used / window * 100)
    return metrics


def read_muse_session(command, sid):
    """MSP session/read is a local read: no attach, writer lease, or model call."""
    from quota import stop_process
    process = subprocess.Popen(command + ['serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        messages.put(message)
                except ValueError:
                    pass
        finally:
            messages.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def send(value):
        process.stdin.write(json.dumps(dict(jsonrpc='2.0', **value)) + '\n')
        process.stdin.flush()
    def request(identity, method, params):
        send(dict(id=identity, method=method, params=params))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Muse usage reader exited')
            if message.get('id') == identity:
                if 'error' in message:
                    raise RuntimeError('Muse session usage unavailable')
                return message.get('result') or {}
            if 'id' in message and 'method' in message:
                send(dict(id=message['id'], error=dict(code=-32601, message='Read-only usage reader')))
        raise TimeoutError('Muse usage reader timed out')
    try:
        request(1, 'initialize', dict(clientInfo=dict(name='agent_tracker', version='1.0'),
            capabilities=dict(requestedCapabilities=[], userInputDialogs=False)))
        send(dict(method='initialized', params={}))
        request(2, 'session/read', dict(sessionId=sid, excludeItems=True))
        # session/read may serve inline history, which has no cumulative state.
        # Read the latest durable view events instead; each tokenUsage event
        # already carries the server's deduplicated session totals and price.
        state = {}
        peer_messages = []
        cursor = None
        for index in range(20):
            params = dict(sessionId=sid, direction='backward', limit=1000, maxBytes=262144)
            if cursor:
                params['cursor'] = cursor
            page = request(index + 3, 'view/page', params)
            for event in reversed(page.get('events') or []):
                def peer_items(value):
                    if not isinstance(value, dict):
                        return
                    peer = value.get('peerMessage') or {}
                    if isinstance(peer, dict) and peer.get('senderSessionId') and not peer.get('remote'):
                        yield peer['senderSessionId']
                    for nested in value.values():
                        if isinstance(nested, dict):
                            yield from peer_items(nested)
                params = event.get('params') or {}
                for sender in peer_items(params):
                    cursor_id = params.get('viewCursor')
                    if cursor_id:
                        peer_messages.append(dict(id=cursor_id, sender=sender))
                if event.get('method') == 'session/tokenUsage' and 'tokenUsage' not in state:
                    state['tokenUsage'] = (event.get('params') or {}).get('cumulative') or {}
                if event.get('method') == 'session/contextUsage' and 'contextUsage' not in state:
                    state['contextUsage'] = event.get('params') or {}
            cursor = page.get('nextCursor')
            if not cursor or ('tokenUsage' in state and 'contextUsage' in state):
                break
        metrics = muse_snapshot_metrics(dict(history=dict(snapshot=dict(state=state))))
        metrics['peer_messages'] = peer_messages
        return metrics
    finally:
        stop_process(process)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()


class MuseSessionUsage:
    def __init__(self):
        self.cache, self.pending, self.lock = {}, set(), threading.Lock()

    def get(self, sid):
        with self.lock:
            metrics, checked = self.cache.get(sid, ({}, 0))
            if sid not in self.pending and time.time() - checked > 15:
                self.pending.add(sid)
                threading.Thread(target=self.refresh, args=(sid,), daemon=True).start()
            return dict(metrics)

    def refresh(self, sid):
        from quota import cli_command
        metrics = None
        try:
            command = cli_command('muse')
            if command:
                metrics = read_muse_session(command, sid)
                metrics['updated_at'] = time.time()
        except Exception:
            # A traceback from this daemon thread would print over the full-screen UI.
            metrics = None
        finally:
            with self.lock:
                if metrics is None:
                    metrics = dict(self.cache.get(sid, ({}, 0))[0], cost_partial=True)
                self.cache[sid] = metrics, time.time()
                self.pending.discard(sid)


class CodexSessionUsage:
    """Incrementally read complete rollout rows; never sum repeated snapshots."""
    def __init__(self):
        self.files = {}

    def read(self, path):
        path = Path(path)
        try:
            stat = path.stat()
            state = self.files.get(path)
            if state is None or stat.st_size < state['offset'] or (
                    stat.st_size == state['offset'] and stat.st_mtime_ns != state['mtime']):
                state = dict(offset=0, mtime=0, totals={}, metrics={})
                self.files[path] = state
            if stat.st_mtime_ns != state['mtime'] or stat.st_size != state['offset']:
                with path.open('rb') as stream:
                    stream.seek(state['offset'])
                    while True:
                        line = stream.readline()
                        if not line or not line.endswith(b'\n'):
                            break
                        state['offset'] = stream.tell()
                        try:
                            self.consume(state, json.loads(line))
                        except (ValueError, TypeError, AttributeError):
                            continue
                state['mtime'] = stat.st_mtime_ns
            return dict(state['metrics'], updated_at=stat.st_mtime)
        except OSError:
            return {}

    @staticmethod
    def consume(state, row):
        payload = row.get('payload') or {}
        if row.get('type') != 'event_msg' or payload.get('type') != 'token_count':
            return
        info = payload.get('info') or {}
        totals = info.get('total_token_usage')
        if not isinstance(totals, dict):
            return
        fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
        if any(not number(totals.get(k, 0)) for k in fields):
            return
        previous = state['totals']
        # Repeated cumulative snapshots do not add tokens again.
        state['totals'] = {k: max(totals.get(k, 0), previous.get(k, 0)) for k in fields}
        metrics = state['metrics']
        metrics['total_tokens'] = sum(state['totals'][k] for k in ('input_tokens', 'output_tokens'))
        last = info.get('last_token_usage') or {}
        window = info.get('model_context_window')
        used = last.get('total_tokens')
        if number(window) and window > 0 and number(used):
            metrics['context_percent'] = min(100, used / window * 100)
            metrics['context_approximate'] = True



def session_metric_lines(metrics):
    lines = []
    percent = metrics.get('context_percent')
    if number(percent):
        lines.append(f"Context {'~' if metrics.get('context_approximate') else ''}{percent:.1f}%")
    if number(metrics.get('total_tokens')):
        lines.append(f"Session tokens: {metrics['total_tokens']:,.0f}")
    cost = metrics.get('cost_usd')
    if number(cost):
        lines.append(f"Reported session cost: ${cost:.4f}" + (' (partial)' if metrics.get('cost_partial') else ''))
    return [' • '.join(lines)] if lines else []
