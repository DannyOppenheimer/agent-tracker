"""Read local message evidence and group live sessions until three human prompts."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import unicodedata


MESSAGE_TOOLS = {'sendmessage', 'send_input', 'send_message', 'send_subagent_message',
                 'send_message_to_thread', 'session_message'}


def mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def stamp(value):
    if isinstance(value, (int, float)):
        while value > 100_000_000_000:
            value /= 1000
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0


def unpack(row, depth=0):
    if not isinstance(row, dict) or depth > 8:
        return
    if row.get('retained_frame'):
        for child in row.get('children') or []:
            try:
                yield from unpack(json.loads(child['record_json']), depth+1)
            except (TypeError, KeyError, ValueError):
                continue
    else:
        yield row


def target(arguments):
    args = mapping(arguments)
    # Idle subscriptions and shutdown requests are not exchanged messages.
    if args.get('type') in ('shutdown_request', 'shutdown_response', 'plan_approval_response'):
        return None
    if not any(args.get(k) for k in ('content', 'message', 'body', 'input')):
        return None
    value = next((args[k] for k in ('recipient', 'to', 'target', 'threadId', 'thread_id',
                                   'subagent_id', 'agent_id', 'id') if args.get(k)), None)
    if isinstance(value, dict):
        value = value.get('session_id') or value.get('sessionId') or value.get('name')
    return value if isinstance(value, str) and value not in ('*', 'all', 'broadcast', 'parent') else None


def failed(result):
    if isinstance(result, list):
        return any(failed(part.get('text')) for part in result if isinstance(part, dict))
    parsed = mapping(result)
    if parsed:
        return bool(parsed.get('error') or parsed.get('isError') or parsed.get('is_error') or
                    parsed.get('success') is False or parsed.get('status') in
                    ('failed', 'refused', 'not_delivered', 'unconfirmed'))
    return isinstance(result, str) and bool(re.match(
        r'\s*(not sent\b|error\b|failed\b|not delivered\b|refused\b)', result, re.I))


def is_peer_text(text):
    return isinstance(text, str) and text.lstrip().startswith((
        '<teammate-message', '<peer-message', '<agent-message', '<agent_notification',
        'Message Type: MESSAGE', 'Message Type: FINAL_ANSWER'))


class LogEvents:
    def __init__(self):
        self.files = {}

    def read(self, path, provider):
        if not path:
            return []
        path = Path(path)
        try:
            stat = path.stat()
            cache = self.files.setdefault((provider, path), dict(offset=0, pending={}, seen=set(), mtime=0))
            # Re-read replaced/truncated logs; stable row ids prevent duplicate events.
            if stat.st_size < cache['offset'] or (stat.st_size == cache['offset'] and
                                                   stat.st_mtime_ns != cache['mtime']):
                cache['offset'] = 0
            events = []
            with path.open('rb') as stream:
                stream.seek(cache['offset'])
                while line := stream.readline():
                    if not line.endswith(b'\n'):
                        break
                    position = cache['offset']
                    cache['offset'] = stream.tell()
                    try:
                        rows = list(unpack(json.loads(line)))
                    except (ValueError, UnicodeError):
                        continue
                    for child_index, row in enumerate(rows):
                        identity = row.get('uuid') or row.get('id') or (position, child_index)
                        if identity in cache['seen']:
                            continue
                        cache['seen'].add(identity)
                        when = stamp(row.get('timestamp') or row.get('recorded_at') or
                                     mapping(row.get('params')).get('timestamp'))
                        events.extend((when, kind, peer) for kind, peer in self.row(row, provider, cache))
            cache['mtime'] = stat.st_mtime_ns
            return events
        except OSError:
            return []

    @staticmethod
    def row(row, provider, cache):
        payload = mapping(row.get('payload'))
        params = mapping(row.get('params'))
        # Confirmed tool responses are used instead of treating tool attempts as sends.
        content = mapping(row.get('message')).get('content')
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    name = str(block.get('name', '')).split('.')[-1].split('__')[-1].lower()
                    peer = target(block.get('input')) if name in MESSAGE_TOOLS else None
                    if peer and block.get('id'):
                        cache['pending'][block['id']] = peer
                elif block.get('type') == 'tool_result':
                    peer = cache['pending'].pop(block.get('tool_use_id'), None)
                    if peer and not block.get('is_error') and not failed(block.get('content')):
                        yield 'message', peer
        if provider == 'Claude' and row.get('type') == 'user':
            text = content if isinstance(content, str) else ' '.join(
                b.get('text', '') for b in (content or []) if isinstance(b, dict) and b.get('type') == 'text')
            match = re.match(r'\s*<(?:teammate|peer|agent)-message\b[^>]*\b(?:teammate_id|sender|sender_id|sender_session_id|senderSessionId|from)=["\']([^"\']+)', text)
            if match:
                yield 'message', match[1]
            elif (text.strip() and not row.get('isMeta') and not row.get('isCompactSummary') and
                  not text.lstrip().startswith(('<', '[Request interrupted', 'This session is being continued')) and
                  not is_peer_text(text)):
                yield 'prompt', None
        elif provider == 'Codex':
            if row.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                if not is_peer_text(payload.get('message')) and not payload.get('is_meta') and not payload.get('sender'):
                    yield 'prompt', None
            if row.get('type') == 'response_item':
                name = str(payload.get('name', '')).split('.')[-1].split('__')[-1].lower()
                if payload.get('type') in ('function_call', 'custom_tool_call') and name in MESSAGE_TOOLS:
                    peer = target(payload.get('arguments') or payload.get('input'))
                    if peer and payload.get('call_id'):
                        cache['pending'][payload['call_id']] = peer
                elif payload.get('type') in ('function_call_output', 'custom_tool_call_output'):
                    peer = cache['pending'].pop(payload.get('call_id'), None)
                    if peer and not failed(payload.get('output')):
                        yield 'message', peer
        elif provider == 'Muse':
            event = mapping(payload.get('event')) or mapping(payload.get('record'))
            if payload.get('kind') == 'run' or row.get('payload_type') == 'runtime.session':
                if event.get('kind') == 'started' and event.get('prompt') and not event.get('sender_session_id'):
                    if not is_peer_text(event.get('prompt')):
                        yield 'prompt', None
            # Muse's stable peerMessage item identifies the local sending session.
            def peers(value, depth=0):
                if not isinstance(value, dict) or depth > 8:
                    return
                peer = mapping(value.get('peerMessage'))
                if peer.get('senderSessionId') and not peer.get('remote'):
                    yield peer['senderSessionId']
                for item in value.values():
                    if isinstance(item, dict):
                        yield from peers(item, depth+1)
            for peer in peers(payload):
                yield 'message', peer
        elif provider == 'Grok':
            update = mapping(params.get('update') or row.get('update'))
            kind = update.get('sessionUpdate')
            if kind in ('agent_thought_chunk', 'agent_message_chunk', 'turn_completed', 'turn_started',
                        'turn_cancelled', 'turn_failed', 'tool_call'):
                cache['prompt_open'] = False
            if kind == 'user_message_chunk' and not mapping(update.get('_meta')).get('senderAgentId'):
                # ACP chunks with the same message id count as one human prompt.
                mid = update.get('messageId') or params.get('messageId')
                text = mapping(update.get('content')).get('text')
                if not is_peer_text(text) and ((mid and mid != cache.get('prompt_id')) or
                                                (not mid and not cache.get('prompt_open'))):
                    cache['prompt_id'] = mid
                    cache['prompt_open'] = True
                    yield 'prompt', None
            if kind in ('tool_call', 'tool_call_update'):
                call = update.get('toolCallId')
                raw = mapping(update.get('rawInput'))
                name = str(raw.get('name') or update.get('name') or update.get('title') or '').split('(')[0].strip()
                if name == 'send_subagent_message' and call:
                    peer = target(raw.get('arguments') or raw)
                    if peer:
                        cache['pending'][call] = peer
                if update.get('status') in ('completed', 'failed'):
                    peer = cache['pending'].pop(call, None)
                    if peer and update['status'] == 'completed' and not failed(update.get('rawOutput')):
                        yield 'message', peer


class MessageGroups:
    def __init__(self):
        self.logs = LogEvents()
        self.counts = defaultdict(int)
        self.baselines = {}
        self.edges = set()
        self.peer_seen = {}

    def update(self, sessions):
        live = {(s.get('provider', 'Claude'), s['session']): s for s in sessions if s.get('session')}
        aliases = defaultdict(set)
        for key, session in live.items():
            for alias in (key[1], session.get('name'), *session.get('message_aliases', [])):
                if alias:
                    aliases[(key[0], str(alias).casefold())].add(key)
        def resolve(sender, address):
            candidates = aliases.get((sender[0], address.casefold()), set())
            if not candidates:
                # Claude's disambiguated address can contain a short session id.
                tokens = re.findall(r'[a-zA-Z0-9_-]{8,}', address)
                candidates = {k for k in live if k[0] == sender[0] and any(
                    k[1].startswith(token) or k[1] in token for token in tokens)}
            return next(iter(candidates)) if len(candidates) == 1 else None
        events = []
        for key, session in live.items():
            for when, kind, peer in self.logs.read(session.get('message_log'), key[0]):
                events.append((when, len(events), key, kind, peer))
        for _, _, key, kind, address in sorted(events):
            if kind == 'prompt':
                self.counts[key] += 1
                if key in self.baselines and self.counts[key] - self.baselines[key] >= 3:
                    self.edges = {edge for edge in self.edges if key not in edge}
                    self.baselines.pop(key, None)
            else:
                peer = resolve(key, address)
                if peer is not None and peer != key:
                    self.edges.add(frozenset((key, peer)))
                    for member in (key, peer):
                        self.baselines[member] = self.counts[member]
        for key, session in live.items():
            if 'peer_messages' not in (session.get('usage') or {}):
                continue
            records = session['usage']['peer_messages']
            # The first MSP read establishes the cursor baseline, so old inbox
            # history cannot revive an exchange after three newer user prompts.
            first_read = key not in self.peer_seen
            seen = self.peer_seen.setdefault(key, set())
            for record in records:
                identity, sender = record.get('id'), record.get('sender')
                if identity in seen or not identity or not sender:
                    continue
                seen.add(identity)
                peer = resolve(key, sender)
                if not first_read and peer is not None and peer != key:
                    self.edges.add(frozenset((key, peer)))
                    for member in (key, peer):
                        self.baselines[member] = self.counts[member]
        self.edges = {edge for edge in self.edges if edge <= live.keys()}
        neighbors = defaultdict(set)
        for edge in self.edges:
            a, b = tuple(edge)
            neighbors[a].add(b)
            neighbors[b].add(a)
        visited = set()
        for key, session in live.items():
            session.pop('message_group', None)
        for root in neighbors:
            if root in visited:
                continue
            members, pending = set(), [root]
            while pending:
                node = pending.pop()
                if node not in members:
                    members.add(node)
                    pending.extend(neighbors[node] - members)
            visited.update(members)
            identity = '|'.join(provider + ':' + sid for provider, sid in sorted(members))
            for key in members:
                live[key]['message_group'] = identity


def session_blocks(sessions):
    groups = defaultdict(list)
    for session in sessions:
        if session.get('message_group'):
            groups[session['message_group']].append(session)
    shown = set()
    for session in sessions:
        group = session.get('message_group')
        if group and len(groups[group]) > 1:
            if group not in shown:
                shown.add(group)
                yield groups[group]
        else:
            yield [session]


def fit_terminal(text, width):
    """Clip/pad a colored row by terminal cells, preserving ANSI colors."""
    parts, cells = [], 0
    for token in re.findall(r'\x1b\[[0-9;]*m|[^\x1b]', text):
        if token.startswith('\x1b'):
            parts.append(token)
            continue
        size = 0 if unicodedata.combining(token) else 2 if unicodedata.east_asian_width(token) in ('W', 'F') else 1
        if cells + size > width:
            break
        parts.append(token)
        cells += size
    return ''.join(parts) + '\x1b[0m' + ' ' * max(0, width-cells)
