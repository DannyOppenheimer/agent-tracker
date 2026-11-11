"""Full-screen, local Claude Code session dashboard. No dependencies."""
import argparse
from itertools import zip_longest
import ctypes
from datetime import datetime, timezone
from contextlib import closing
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import subprocess
import textwrap
import tempfile
import time
import tomllib
from urllib.parse import quote, urlsplit
from urllib.request import url2pathname
from usage import Usage, antigravity_home, capture_claude, capture_antigravity, usage_panels, usage_pages, demo_usage
from session_usage import CodexSessionUsage, MuseSessionUsage, session_metric_lines
from message_groups import MessageGroups, session_blocks, fit_terminal
from titles import heuristic_title

HOME = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
E = '\x1b['
ORANGE = E + '38;2;238;145;87m'
BLUE = E + '38;2;112;181;255m'
PURPLE = E + '38;2;196;143;255m'
PINK = E + '38;2;255;153;204m'
LIGHT_GRAY = E + '38;2;170;170;170m'
PROVIDER_COLORS = {'Claude': ORANGE, 'Codex': BLUE, 'Antigravity': PURPLE,
                   'Muse': PINK, 'Grok': LIGHT_GRAY}
RESET = E + '0m'
DIM = E + '2m'


def configure_usage_collectors():
    """Connect installed agents when they have no existing status line."""
    launcher = [sys.executable]
    if not getattr(sys, 'frozen', False):
        launcher.append(str(Path(__file__).resolve()))
    command = ' '.join(f'"{part}"' for part in launcher)
    providers = (
        ('Claude', HOME, shutil.which('claude'), 'claude', {}),
        ('Antigravity', antigravity_home(), shutil.which('agy'), 'antigravity',
         {'stack_with_default': True}),
    )
    messages = []
    for name, folder, executable, flag, extra in providers:
        if not executable and not folder.is_dir():
            continue
        settings = folder / 'settings.json'
        try:
            original = settings.read_bytes() if settings.is_file() else None
            data = json.loads(original.decode('utf-8-sig')) if original is not None else {}
            if not isinstance(data, dict) or 'statusLine' in data:
                continue
            data['statusLine'] = dict(type='command', command=f'{command} --capture-{flag}-usage', **extra)
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            try:
                current = settings.read_bytes() if settings.is_file() else None
                if current == original:
                    temporary.replace(settings)
                    messages.append(f'Reopen {name} to start collecting usage')
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            messages.append(f'Could not connect {name} usage')
    grok_home = Path(os.environ.get('GROK_HOME') or Path.home() / '.grok')
    if shutil.which('grok') or grok_home.is_dir():
        settings = grok_home / 'config.toml'
        try:
            original = settings.read_bytes() if settings.is_file() else b''
            data = tomllib.loads(original.decode('utf-8-sig'))
            if 'status_line' not in data.get('ui', {}):
                addition = '\n[ui.status_line]\ntype = "command"\ncommand = ' + json.dumps(
                    command + ' --capture-grok-usage') + '\n'
                updated = original + addition.encode('utf-8')
                # Appending a table is invalid when `ui` is an inline table or
                # defined with dotted keys; never write a config Grok cannot load.
                status = tomllib.loads(updated.decode('utf-8-sig')).get('ui', {}).get('status_line', {})
                if not isinstance(status, dict) or status.get('type') != 'command':
                    raise ValueError('Grok config cannot take an appended status line')
                grok_home.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile('wb', dir=grok_home, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(updated)
                try:
                    current = settings.read_bytes() if settings.is_file() else b''
                    if current == original:
                        temporary.replace(settings)
                        messages.append('Reopen Grok to start collecting session usage')
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, AttributeError):
            messages.append('Could not connect Grok session usage; add [ui.status_line] to config.toml manually')
    return messages

def clean(text):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(text)).strip()

def alive(record):
    pid = int(record.get('pid', 0))
    if pid <= 0:
        return False
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        times = [W.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return False
        created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        return not record.get('procStart') or created == int(record['procStart'])
    finally:
        k.CloseHandle(handle)

def records(path, tail=False):
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144) if tail else 0)
            if tail and size > 262144:
                f.readline()
            data = f.read(262144)
        result = []
        for line in data.splitlines():
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeError):
                pass
        return result
    except OSError:
        return []

def activity(tool):
    tool = tool.lower()
    for names, state in [(('edit', 'write', 'notebook', 'patch'), 'editing'),
                         (('read', 'glob', 'grep', 'search', 'fetch'), 'reading'),
                         (('bash', 'shell', 'terminal'), 'running')]:
        if any(n in tool for n in names):
            return state
    return 'working'

def user_text(row):
    if row.get('type') != 'user' or row.get('isMeta') or row.get('isCompactSummary'):
        return ''
    content = (row.get('message') or {}).get('content', [])
    text = content if isinstance(content, str) else ' '.join(
        b.get('text', '') for b in content if b.get('type') == 'text')
    if text.lstrip().startswith(('<', '[Request interrupted', 'This session is being continued')):
        return ''
    return text.strip()


def prompt_timestamp(value):
    """Normalize ISO dates and epoch seconds/milliseconds/microseconds to seconds."""
    try:
        number = float(value)
        if not math.isfinite(number):
            return 0  # inf would never shrink below the epoch-seconds threshold.
        while number > 100_000_000_000:
            number /= 1000
        return number if number > 0 else 0
    except (ValueError, TypeError, OverflowError):
        try:
            date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            return 0


class PromptTimes:
    """Read prompt events incrementally, retaining timestamps through long tool runs."""
    def __init__(self):
        self.cache = {}

    def read(self, path, provider):
        if path is None:
            return 0
        path = Path(path)
        previous = self.cache.get(path)
        try:
            st = path.stat()
            key = st.st_mtime_ns, st.st_size
            if previous and previous[0] == key:
                return previous[2]
            offset, latest = (previous[1], previous[2]) if previous and st.st_size > previous[0][1] else (0, 0)
            with path.open('rb') as stream:
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b'\n'):
                        break  # Retry incomplete records after the writer finishes.
                    offset = stream.tell()
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    for stamp in self.events(row, provider):
                        # Untimed events can be ordered once observed during a refresh.
                        latest = max(latest, stamp or (time.time() if previous else 0))
            self.cache[path] = key, offset, latest
            return latest
        except OSError:
            return previous[2] if previous else 0

    def events(self, row, provider, depth=0):
        if not isinstance(row, dict) or depth > 8:
            return
        if row.get('retained_frame'):
            for child in row.get('children') or []:
                try:
                    yield from self.events(json.loads(child['record_json']), provider, depth+1)
                except (ValueError, TypeError, KeyError):
                    continue
            return
        payload = row.get('payload') or {}
        params = row.get('params') or {}
        if not isinstance(payload, dict) or not isinstance(params, dict):
            return
        if provider == 'Claude':
            is_prompt = bool(user_text(row))
        elif provider == 'Codex':
            is_prompt = (row.get('type') == 'event_msg' and payload.get('type') == 'user_message')
        elif provider == 'Muse':
            event = payload.get('event') or {}
            is_prompt = (row.get('payload_type') == 'runtime.session' and
                         isinstance(event, dict) and event.get('kind') == 'started' and bool(event.get('prompt')))
        else:
            update = params.get('update') or row.get('update') or {}
            is_prompt = isinstance(update, dict) and update.get('sessionUpdate') == 'user_message_chunk'
        if is_prompt:
            yield prompt_timestamp(row.get('timestamp') or row.get('recorded_at') or params.get('timestamp'))


def guarded_scan(backend, provider):
    """One malformed log or schema change must not close the whole dashboard."""
    try:
        return backend.scan()
    except Exception:
        backend.error = f'{provider} sessions temporarily unavailable (unexpected data)'
        return []


def scan_all(backends):
    sessions = [session for backend, provider in backends for session in guarded_scan(backend, provider)]
    sort_sessions(sessions)
    return sessions


def sort_sessions(sessions):
    sessions.sort(key=lambda s: (-s.get('last_prompt_at', 0),
                                 s.get('provider', '').casefold() if not s.get('last_prompt_at') else '',
                                 s['directory'],
                                 s.get('provider', ''), s.get('session', '')))

class TranscriptTitles:
    """Read Claude transcripts incrementally for titles; no model calls.

    Priority: the user's /rename title, then Claude Code's own `ai-title` rows
    (refreshed as the chat evolves), an older compaction summary, and finally an
    offline extract of the first substantive request.
    """
    def __init__(self):
        self.files = {}

    def read(self, path):
        path = Path(path)
        size = path.stat().st_size
        state = self.files.get(path)
        if state is None or size < state['offset']:
            state = self.files[path] = dict(offset=0, custom='', ai='', summary='', prompts=[])
        with path.open('rb') as stream:
            stream.seek(state['offset'])
            while line := stream.readline():
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    if line.endswith(b'\n'):
                        state['offset'] = stream.tell()
                        continue
                    break  # Retry a record the writer has not finished.
                state['offset'] = stream.tell()
                if not isinstance(row, dict):
                    continue
                kind = row.get('type')
                for key, field, row_kind in (('custom', 'customTitle', 'custom-title'),
                                             ('ai', 'aiTitle', 'ai-title'), ('summary', 'summary', 'summary')):
                    if kind == row_kind and isinstance(row.get(field), str) and clean(row[field]):
                        state[key] = clean(row[field])
                if len(state['prompts']) < 5:
                    try:
                        text = user_text(row)
                    except (AttributeError, TypeError):
                        text = ''
                    if text:
                        state['prompts'].append(text[:5000])
        title = (state['custom'] or state['ai'] or state['summary'] or
                 heuristic_title(state['prompts']) or 'Task not available')
        return textwrap.shorten(clean(title), width=160, placeholder='…')


def codex_command():
    executable = shutil.which('codex.exe') or shutil.which('codex')
    if executable and Path(executable).suffix.lower() not in ('.cmd', '.bat', '.ps1'):
        return [executable]
    # Invoke the native npm binary directly, avoiding cmd.exe and PowerShell quoting.
    shim = shutil.which('codex.cmd') or executable
    if shim:
        package = Path(shim).parent / 'node_modules' / '@openai' / 'codex'
        binaries = list(package.glob('node_modules/@openai/codex-*/vendor/*/bin/codex.exe'))
        binaries += list(package.glob('vendor/*/codex/codex.exe'))
        if binaries:
            return [str(binaries[0])]
        node = shutil.which('node')
        script = package / 'bin' / 'codex.js'
        if node and script.is_file():
            return [node, str(script)]
    raise RuntimeError('Codex CLI not found')


class Tracker:
    def __init__(self, home=HOME):
        self.home, self.paths, self.cache = home, {}, {}
        self.prompt_times = PromptTimes()
        self.titles = TranscriptTitles()
        self.error = ''

    def details(self, record):
        sid = record['sessionId']
        path = self.paths.get(sid)
        if path is None or not path.exists():
            path = next((p / (sid + '.jsonl') for p in (self.home / 'projects').glob('*') if (p / (sid + '.jsonl')).is_file()), None)
            if path:
                self.paths[sid] = path
        if not path:
            return 'No transcript yet', 'waiting', ''
        st = path.stat()
        key = st.st_mtime_ns, st.st_size, record.get('status')
        if path in self.cache and self.cache[path][0] == key:
            return self.cache[path][1]
        detail = ''
        state = 'thinking'
        for row in records(path, True):
            content = (row.get('message') or {}).get('content', [])
            if row.get('type') == 'user' and not row.get('isMeta'):
                text = content if isinstance(content, str) else ' '.join(b.get('text', '') for b in content if b.get('type') == 'text')
                if text and not text.startswith(('<', '[Request interrupted')):
                    state, detail = 'thinking', ''
            if row.get('type') == 'assistant' and isinstance(content, list):
                for b in content:
                    if b.get('type') == 'tool_use':
                        tool = b.get('name', '')
                        inp = b.get('input') or {}
                        state = activity(tool)
                        detail = tool + ': ' + str(inp.get('file_path') or inp.get('command') or inp.get('pattern') or '')
                    elif b.get('type') in ('thinking', 'text'):
                        state, detail = ('thinking' if b['type'] == 'thinking' else 'responding'), ''
            if row.get('subtype') == 'turn_duration':
                state, detail = 'waiting', ''
        if record.get('status') == 'idle':
            state, detail = 'waiting', ''
        result = self.titles.read(path), state, clean(detail)
        self.cache[path] = key, result
        return result

    def scan(self):
        self.error = ''
        sessions = []
        for path in (self.home / 'sessions').glob('*.json'):
            try:
                r = json.loads(path.read_text(encoding='utf-8'))
                if not r.get('sessionId') or not alive(r):
                    continue
                task, state, detail = self.details(r)
                sessions.append(dict(provider='Claude', pid=r['pid'], session=r['sessionId'], name=clean(r.get('name', 'Claude')),
                    directory=clean(r.get('cwd', 'Unknown')), task=task, activity=state, detail=detail,
                    last_prompt_at=self.prompt_times.read(self.paths.get(r['sessionId']), 'Claude'),
                    message_log=str(self.paths[r['sessionId']]) if r['sessionId'] in self.paths else None))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        sort_sessions(sessions)
        return sessions

def thread_locked(path):
    """Probe an existing lock without acquiring it or modifying the file."""
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    k.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD), W.LPVOID]
    k.CloseHandle.argtypes = [W.HANDLE]
    handle = k.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
    if handle == W.HANDLE(-1).value:
        return False
    try:
        buf, count = ctypes.create_string_buffer(1), W.DWORD()
        ok = k.ReadFile(handle, buf, 1, ctypes.byref(count), None)
        return not ok and ctypes.get_last_error() == 33  # ERROR_LOCK_VIOLATION
    finally:
        k.CloseHandle(handle)

class AntigravityTracker:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else antigravity_home()
        self.error = ''

    def scan(self):
        self.error = ''
        ids = [path.stem for path in (self.home / 'presence').glob('*.lock')
               if not path.name.startswith('.') and thread_locked(path)]
        if not ids:
            return []

        summaries = {}
        database = self.home / 'conversation_summaries.db'
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
                db.row_factory = sqlite3.Row
                placeholders = ','.join('?' for _ in ids)
                rows = db.execute(
                    'SELECT conversation_id, title, workspace_uris, not_fully_idle, last_user_input_time '
                    f'FROM conversation_summaries WHERE conversation_id IN ({placeholders})', ids)
                summaries = {row['conversation_id']: dict(row) for row in rows}
        except sqlite3.Error:
            self.error = 'Antigravity chat details temporarily unavailable'

        sessions = []
        for sid in ids:
            summary = summaries.get(sid, {})
            directory = 'Unknown'
            try:
                workspaces = json.loads(summary.get('workspace_uris', '[]'))
                if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str):
                    uri = urlsplit(workspaces[0])
                    if uri.scheme == 'file':
                        path = ('//' + uri.netloc if uri.netloc else '') + uri.path
                        directory = clean(url2pathname(path)) or directory
            except (ValueError, TypeError):
                pass
            sessions.append(dict(provider='Antigravity', name='Antigravity', pid=None, session=sid,
                                 directory=directory, task=clean(summary.get('title') or 'Untitled chat'),
                                 activity='working' if summary.get('not_fully_idle') else 'waiting', detail='',
                                 last_prompt_at=prompt_timestamp(summary.get('last_user_input_time'))))
        return sessions


class CodexTracker:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        self.cache = {}
        self.prompt_times = PromptTimes()
        self.error = ''
        self.session_usage = CodexSessionUsage()

    def details(self, path):
        try:
            st = path.stat()
        except OSError:
            return 'waiting', ''
        key = st.st_mtime_ns, st.st_size
        if path in self.cache and self.cache[path][0] == key:
            return self.cache[path][1]
        state, detail = 'waiting', ''
        for row in records(path, True):
            payload = row.get('payload') or {}
            kind = payload.get('type')
            if row.get('type') == 'event_msg':
                if kind in ('task_started', 'turn_started', 'user_message'):
                    state, detail = 'thinking', ''
                elif kind in ('task_complete', 'turn_complete', 'task_completed', 'turn_completed', 'turn_aborted'):
                    state, detail = 'waiting', ''
                elif kind == 'agent_reasoning':
                    state, detail = 'thinking', ''
                elif kind == 'agent_message':
                    state, detail = ('waiting' if payload.get('phase') == 'final_answer' else 'responding'), ''
                elif kind == 'item_started':
                    item = payload.get('item') or {}
                    if item.get('type') == 'Reasoning':
                        state, detail = 'thinking', ''
            elif row.get('type') == 'response_item':
                if kind in ('function_call', 'custom_tool_call'):
                    name = payload.get('name', 'tool')
                    args = payload.get('arguments') or payload.get('input') or ''
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            pass
                    state = activity(name)
                    if isinstance(args, dict):
                        detail = str(args.get('cmd') or args.get('command') or args.get('path') or name)
                    else:
                        detail = str(args)
                    # Codex often wraps file operations in exec/exec_command.
                    if name in ('exec', 'exec_command', 'shell', 'shell_command'):
                        state = 'running'
                        if any(x in detail for x in ('apply_patch', '*** Begin Patch')):
                            state = 'editing'
                        elif any(x in detail for x in ('Get-Content', 'rg ', 'rg.exe', 'cat ', 'read_text', 'read_file')):
                            state = 'reading'
                elif kind == 'reasoning':
                    state, detail = 'thinking', ''
                elif kind == 'message' and payload.get('role') == 'assistant':
                    state, detail = ('waiting' if payload.get('phase') == 'final_answer' else 'responding'), ''
        result = state, clean(detail)[:240]
        self.cache[path] = key, result
        return result

    def scan(self):
        self.error = ''
        ids = [p.stem for p in (self.home / 'thread-writer-locks').glob('*.lock')
               if not p.name.startswith('.') and thread_locked(p)]
        if not ids:
            return []
        databases = sorted(self.home.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
        if not databases:
            self.error = 'Codex state database not found'
            return []
        try:
            with closing(sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM threads WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
        except sqlite3.Error:
            self.error = 'Codex state temporarily unavailable'
            return []
        sessions = []
        for row in rows:
            r = dict(row)
            state, detail = self.details(Path(r['rollout_path']))
            sessions.append(dict(provider='Codex', pid=None, session=r['id'],
                name=clean(r.get('agent_nickname') or r.get('name') or 'Codex'),
                directory=clean(r['cwd'].removeprefix('\\\\?\\')),
                task=clean(r.get('name') or r.get('title') or r.get('first_user_message') or 'Task not available'),
                activity=state, detail=detail, usage=self.session_usage.read(Path(r['rollout_path'])),
                last_prompt_at=self.prompt_times.read(Path(r['rollout_path']), 'Codex'),
                message_log=r['rollout_path']))
        return sessions

def json_file(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def transcript_rows(path):
    """Stream complete logs for prompts, tolerating an incomplete final write."""
    def unpack(row, depth=0):
        if not isinstance(row, dict) or depth > 8:
            return
        if row.get('retained_frame'):
            children = row.get('children') or []
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, dict) or not isinstance(child.get('record_json'), str):
                        continue
                    try:
                        yield from unpack(json.loads(child['record_json']), depth+1)
                    except ValueError:
                        continue
        else:
            yield row
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    yield from unpack(row)
                except ValueError:
                    continue
    except OSError:
        return


def tool_activity(call):
    function = call.get('function') or call
    name = str(function.get('name') or call.get('title') or 'tool')
    args = function.get('arguments') or call.get('rawInput') or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    detail = (args.get('file_path') or args.get('path') or args.get('command')
              or args.get('cmd') or args.get('pattern') or '') if isinstance(args, dict) else args
    return activity(name), clean(name + ': ' + str(detail))[:240]


class MuseTracker:
    """Muse's date-sharded event logs; only held .session.lock files are live."""
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else Path(
            os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'muse'
        self.cache, self.error = {}, ''
        self.prompt_times = PromptTimes()
        self.session_usage = MuseSessionUsage()

    def details(self, path):
        st = path.stat()
        key = st.st_mtime_ns, st.st_size
        if path in self.cache and self.cache[path][0] == key:
            return self.cache[path][1]
        directory, state, detail = 'Unknown', 'waiting', ''
        prompts = []
        for row in transcript_rows(path):
            payload = row.get('payload')
            if not isinstance(payload, dict):
                continue
            if row.get('payload_type') == 'runtime.session.metadata':
                metadata = payload.get('record') or payload
                if isinstance(metadata, dict):
                    directory = clean(metadata.get('workspace_root') or directory)
            if row.get('payload_type') != 'runtime.session':
                continue
            event = payload.get('event') or {}
            if not isinstance(event, dict):
                continue
            kind = event.get('kind')
            if kind == 'started':
                prompt = event.get('prompt')
                if isinstance(prompt, str) and prompt.strip():
                    prompts.append(prompt[:5000])
                    # Keep the objective and latest refinements without unbounded memory.
                    if len(prompts) > 5:
                        del prompts[1]
                state, detail = 'thinking', ''
            elif kind == 'assistant_tool_calls_committed':
                calls = event.get('tool_calls') or []
                if isinstance(calls, list) and calls and isinstance(calls[-1], dict):
                    state, detail = tool_activity(calls[-1])
            elif kind == 'assistant_message_committed':
                state, detail = 'responding', ''
            elif kind == 'terminal':
                state, detail = 'waiting', ''
        # Muse records no title of its own; extract one offline from the requests.
        task = textwrap.shorten(clean(heuristic_title(prompts) or 'Task not available'),
                                width=160, placeholder='…')
        result = directory, task, state, detail
        self.cache[path] = key, result
        return result

    def scan(self):
        self.error = ''
        sessions = []
        for lock in (self.home / 'sessions').glob('*/*/*/*/.session.lock'):
            try:
                if not thread_locked(lock):
                    continue
                path = lock.with_name('session.jsonl')
                directory, task, state, detail = self.details(path)
                sid = lock.parent.name
                sessions.append(dict(provider='Muse', name='Muse', pid=None, session=sid,
                                     directory=directory, task=task, activity=state, detail=detail,
                                     usage=self.session_usage.get(sid),
                                     last_prompt_at=self.prompt_times.read(path, 'Muse'), message_log=str(path)))
            except (OSError, ValueError, TypeError, KeyError):
                self.error = 'Muse session details temporarily unavailable'
        return sessions


class GrokTracker:
    """Grok Build's active-session registry and ACP update stream (xai-org/grok-build)."""
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('GROK_HOME') or Path.home() / '.grok')
        self.paths, self.cache, self.error = {}, {}, ''
        self.prompt_times = PromptTimes()

    def details(self, path):
        try:
            st = path.stat()
        except OSError:
            return 'waiting', '', ''
        key = st.st_mtime_ns, st.st_size
        if path in self.cache and self.cache[path][0] == key:
            return self.cache[path][1]
        state, detail, prompt = 'waiting', '', ''
        for row in records(path, True):
            if not isinstance(row, dict):
                continue
            params = row.get('params') or row
            if not isinstance(params, dict):
                continue
            update = params.get('update') or {}
            if not isinstance(update, dict):
                continue
            kind = update.get('sessionUpdate')
            if kind == 'user_message_chunk':
                content = update.get('content') or {}
                if isinstance(content, dict) and content.get('type') == 'text':
                    prompt = clean(content.get('text', '')) or prompt
                state, detail = 'thinking', ''
            elif kind == 'agent_thought_chunk':
                state, detail = 'thinking', ''
            elif kind == 'agent_message_chunk':
                state, detail = 'responding', ''
            elif kind in ('tool_call', 'tool_call_update'):
                status = update.get('status')
                if status in ('completed', 'failed'):
                    state, detail = 'thinking', ''
                elif status == 'pending':
                    state, detail = 'waiting', clean(update.get('title', 'Tool approval'))
                else:
                    tool_kind = update.get('kind')
                    state = {'read': 'reading', 'search': 'reading', 'edit': 'editing',
                             'delete': 'editing', 'execute': 'running'}.get(tool_kind, 'working')
                    detail = clean(update.get('title', ''))[:240]
            elif kind in ('turn_completed', 'turn_cancelled', 'turn_failed'):
                state, detail = 'waiting', ''
        result = state, detail, prompt
        self.cache[path] = key, result
        return result

    def scan(self):
        self.error = ''
        registry = json_file(self.home / 'active_sessions.json', [])
        if not isinstance(registry, list):
            self.error = 'Grok active-session registry temporarily unavailable'
            return []
        sessions, seen = [], set()
        for record in registry:
            try:
                if not isinstance(record, dict) or not record.get('session_id') or not alive(record):
                    continue
                sid = record['session_id']
                if sid in seen:
                    continue
                seen.add(sid)
                path = self.paths.get(sid)
                if path is None or not path.is_dir():
                    path = self.home / 'sessions' / quote(record.get('cwd', ''), safe='') / sid
                    if not path.is_dir():
                        path = next((self.home / 'sessions').glob('*/' + sid), path)
                    self.paths[sid] = path
                summary = json_file(path / 'summary.json', {})
                if not isinstance(summary, dict):
                    summary = {}
                state, detail, prompt = self.details(path / 'updates.jsonl')
                task = summary.get('generated_title') or summary.get('session_summary') or prompt or 'Task not available'
                sessions.append(dict(provider='Grok', name=clean(summary.get('agent_name') or 'Grok'),
                                     pid=record['pid'], session=sid, directory=clean(record.get('cwd') or 'Unknown'),
                                     task=textwrap.shorten(clean(task), width=160, placeholder='…'),
                                     activity=state, detail=detail,
                                     last_prompt_at=self.prompt_times.read(path / 'updates.jsonl', 'Grok'),
                                     message_log=str(path / 'updates.jsonl')))
            except (OSError, ValueError, TypeError, KeyError):
                self.error = 'Grok session details temporarily unavailable'
        return sessions


def mascot(state, frame):
    phase = frame % 4
    # A small marshmallow: soft outline, blush, smile, and tucked-in feet.
    sleepy = state == 'waiting' or frame % 20 == 19
    face = '    │˶ -ᴗ- ˶│' if sleepy else '    │˶ •ᴗ• ˶│'
    head = '    ╭───────╮'
    feet = '      ᵕ   ᵕ'
    if state == 'reading':
        # The hand (つ) occupies two terminal columns; each page is two columns.
        pages = '      │≡ │ ≡│' if phase < 2 else '      │ ≡│≡ │'
        return ['           ' + ('♡' if phase == 2 else ' '), head, face,
                '    つ╭──┬──╮と', pages, '      ╰──┴──╯']
    if state == 'editing':
        # A two-row pencil moves across the right page, leaving ink behind.
        cover = '─╱─' if phase % 2 == 0 else '──╱'
        writing = ['╱  ', '·╱ ', '╱· ', '─╱ '][phase]
        return ['', head, face,
                '    つ╭──┬' + cover + '╮と',
                '      │≡ │' + writing + '│', '      ╰──┴───╯']
    if state == 'running':
        return ['            ' + ['·', '✧', '·', ' '][phase], head, face,
                '    つ╭─────╮と', '      │ >' + ('_ ' if phase < 2 else '  ') + '│',
                '      ╰─────╯']
    if state == 'waiting':
        return ['           ' + ['z', ' z', '  z', ' z'][phase], head, face,
                '    │  つと │', '    ╰───────╯', feet]
    bubble = ['·', '· ·', '· · ·', '· ·'][phase]
    if state == 'responding':
        bubble = ['♡', '♡ ·', '♡', '· ♡'][phase]
        arms = '   つ       │' if phase < 2 else '    │       つ'
    else:
        arms = '    │  つ   │'
    return ['           ' + bubble, head, face, arms, '    ╰───────╯', feet]

def render(sessions, frame, offset, width, height, notice='', usage=None, buttons=None, navigating=None):
    heading = ' + '.join(color + provider.upper() + RESET for provider, color in PROVIDER_COLORS.items())
    lines = ['  ' + heading + '   ' + str(len(sessions)) + ' live sessions',
             DIM + '  Right/Left Arrow: Switch through usage reports'[:max(0, width - 1)] + RESET,
             DIM + '  Up/Down Arrow or Scroll Wheel: Scroll through active sessions'[:max(0, width - 1)] + RESET, '']
    if notice:
        lines[3] = DIM + '  ' + notice[:max(0, width - 4)] + RESET
    if usage:
        lines.extend(usage)
        lines.append('')
    if not sessions:
        lines += ['  No active agent chats.', '  Start an agent in another terminal; it will appear here.']
    targets = []
    for block in session_blocks(sessions):
        boxed = len(block) > 1 and width >= 30
        border = PROVIDER_COLORS.get(block[0].get('provider', 'Claude'), ORANGE)
        if boxed:
            title = ' Exchanged messages '
            lines.append(' ' + border + '\u256d\u2500' + title + '\u2500' * max(0, width-5-len(title)) + '\u256e' + RESET)
        for member_index, s in enumerate(block):
            text_width = max(0, width - (28 if boxed else 24))
            session_start = len(lines)
            provider = s.get('provider', 'Claude')
            color = PROVIDER_COLORS.get(provider, ORANGE)
            identity = f"PID {s['pid']}" if s.get('pid') else s['session'][:8]
            task_lines = textwrap.wrap('Task: ' + s['task'], width=max(1, text_width), max_lines=2, placeholder='…')
            button = '[Go to terminal]'
            is_navigating = navigating == (provider, s.get('session'))
            if is_navigating:
                control_lines = [color + part + RESET for part in textwrap.wrap(
                    '[Navigating to terminal, please wait...]', width=max(1, text_width))]
            elif text_width >= len(button) and s.get('session'):
                targets.append((len(lines) + 2, (25 if boxed else 22), (25 if boxed else 22) + len(button), s))
                control_lines = [color + button + RESET]
            else:
                control_lines = [DIM + '─' * text_width + RESET]
            labels = [E + '1m' + f"{provider} · {s['name']} | {identity}"[:text_width] + RESET,
                      s['directory'][:text_width], *control_lines,
                      task_lines[0], task_lines[1] if len(task_lines) > 1 else '',
                      s['activity'].upper() + ('  ' + s['detail'] if s['detail'] else '')]
            for art, label in zip_longest(mascot(s['activity'], frame), labels, fillvalue=''):
                lines.append(color + (fit_terminal(art, 22) if boxed else art.ljust(22)) + RESET + (label if '\x1b' in label else label[:text_width]))
            for metric in session_metric_lines(s.get('usage') or {}):
                for part in textwrap.wrap(metric, width=max(1, text_width)):
                    lines.append(' ' * min(22, max(0, width - 2)) + DIM + part + RESET)
            if boxed:
                for row_index in range(session_start, len(lines)):
                    lines[row_index] = (' ' + border + '\u2502 ' + RESET +
                                        fit_terminal(lines[row_index], max(1, width-6)) + border + ' \u2502' + RESET)
                if member_index + 1 < len(block):
                    lines.append(' ' + border + '\u2502  ' + RESET + DIM +
                                 '\u2500' * max(1, width-8) + RESET + border + '  \u2502' + RESET)
            else:
                lines += [DIM + '  ' + '\u2501' * max(0, width - 5) + RESET, '']
        if boxed:
            lines += [' ' + border + '\u2570' + '\u2500' * max(1, width-4) + '\u256f' + RESET, '']
    viewport = max(1, height - 1)
    offset = min(max(0, offset), max(0, len(lines) - viewport))
    if buttons is not None:
        buttons[:] = [(row-offset, start, end, session) for row, start, end, session in targets
                      if offset <= row < offset + viewport]
    return '\r\n'.join(line + E + 'K' for line in lines[offset:offset + viewport]) + E + 'J', offset


class ConsoleInput:
    """Read key and wheel events together so mouse events cannot clog the queue."""
    class Key(ctypes.Structure):
        _fields_ = [('down', W.BOOL), ('repeat', W.WORD), ('virtual', W.WORD),
                    ('scan', W.WORD), ('char', W.WCHAR), ('control', W.DWORD)]
    class Mouse(ctypes.Structure):
        _fields_ = [('x', W.SHORT), ('y', W.SHORT), ('buttons', W.DWORD),
                    ('control', W.DWORD), ('flags', W.DWORD)]

    def __init__(self, kernel, handle):
        class Data(ctypes.Union):
            _fields_ = [('key', ConsoleInput.Key), ('mouse', ConsoleInput.Mouse),
                        ('padding', ctypes.c_byte * 16)]
        class Event(ctypes.Structure):
            _fields_ = [('kind', W.WORD), ('data', Data)]
        self.Event, self.kernel, self.handle = Event, kernel, handle
        kernel.GetNumberOfConsoleInputEvents.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
        kernel.ReadConsoleInputW.argtypes = [W.HANDLE, ctypes.POINTER(Event), W.DWORD, ctypes.POINTER(W.DWORD)]
        self.wheel = 0
        self.left_down = False

    def actions(self):
        count = W.DWORD()
        if not self.kernel.GetNumberOfConsoleInputEvents(self.handle, ctypes.byref(count)) or not count.value:
            return []
        events = (self.Event * min(count.value, 128))()
        read = W.DWORD()
        if not self.kernel.ReadConsoleInputW(self.handle, events, len(events), ctypes.byref(read)):
            return []
        actions = []
        for event in events[:read.value]:
            if event.kind == 1 and event.data.key.down:
                key = event.data.key
                if key.char in ('\x03', 'q', 'Q'):
                    raise KeyboardInterrupt
                action = {38: 'up', 40: 'down', 33: 'page_up', 34: 'page_down',
                          36: 'home', 35: 'end', 37: 'left', 39: 'right'}.get(key.virtual)
                if action:
                    actions.extend([action] * max(1, key.repeat))
            elif event.kind == 2 and event.data.mouse.flags & 4:
                self.wheel += ctypes.c_short(event.data.mouse.buttons >> 16).value
                while abs(self.wheel) >= 120:
                    actions.extend(['up' if self.wheel > 0 else 'down'] * 3)
                    self.wheel += -120 if self.wheel > 0 else 120
            elif event.kind == 2:
                mouse = event.data.mouse
                pressed = bool(mouse.buttons & 1)
                if pressed and not self.left_down and mouse.flags == 0:
                    actions.append(('click', mouse.x, mouse.y))
                self.left_down = pressed
        return actions

def main(argv=None, preview=None):
    """Run the dashboard, optionally using an injected, isolated preview snapshot."""
    parser = argparse.ArgumentParser(prog='agent-tracker', description='Local AI agent sessions and usage dashboard')
    parser.add_argument('--json', action='store_true', help='Print live sessions once as JSON')
    parser.add_argument('--demo', action='store_true', help='Preview with sample sessions')
    parser.add_argument('--capture-claude-usage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--usage-json', action='store_true', help='Print account usage')
    parser.add_argument('--setup-usage', action='store_true', help='Connect status-line usage collectors')
    parser.add_argument('--capture-antigravity-usage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--capture-grok-usage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--focus-agent', nargs=2, metavar=('PROVIDER', 'SESSION'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if preview is not None:
        if any((args.demo, args.setup_usage, args.capture_claude_usage,
                args.capture_antigravity_usage, args.capture_grok_usage, args.focus_agent)):
            parser.error('Preview data cannot be combined with agent actions or --demo.')
        if args.json or args.usage_json:
            print(json.dumps(preview['usage'] if args.usage_json else preview['sessions'], indent=2))
            return
    if args.focus_agent:
        from terminal_focus import focus_agent
        try:
            focus_agent(*args.focus_agent)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(1)
        return
    if args.setup_usage:
        print('\n'.join(configure_usage_collectors()) or 'No status-line changes needed.')
        return
    if args.capture_antigravity_usage:
        try:
            capture_antigravity(json.load(sys.stdin))
        except (OSError, ValueError, TypeError):
            pass
        return
    if args.capture_claude_usage:
        try:
            capture_claude(json.load(sys.stdin), HOME)
        except (OSError, ValueError, TypeError):
            pass
        return
    if args.capture_grok_usage:
        try:
            capture_claude(json.load(sys.stdin), Path(os.environ.get('GROK_HOME') or Path.home() / '.grok'))
        except (OSError, ValueError, TypeError):
            pass
        return
    if args.usage_json:
        usage = Usage(HOME, codex_command, start=False)
        usage.refresh_all()
        print(json.dumps(usage.snapshot(), indent=2))
        return
    if preview is None:
        tracker = Tracker()
        codex = CodexTracker()
        antigravity = AntigravityTracker()
        muse = MuseTracker()
        grok = GrokTracker()
        backends = [(tracker, 'Claude'), (codex, 'Codex'), (antigravity, 'Antigravity'),
                    (muse, 'Muse'), (grok, 'Grok')]
    if args.json:
        sessions = scan_all(backends)
        usage = Usage(HOME, codex_command, start=False)
        usage.observe_sessions(sessions)
        MessageGroups().update(sessions)
        print(json.dumps(sessions, indent=2))
        for backend, _ in backends:
            if backend.error:
                print(backend.error, file=sys.stderr)
        return
    if not sys.stdout.isatty():
        parser.error('Use an interactive terminal, or pass --json.')
    setup = configure_usage_collectors() if preview is None and not args.demo else []
    usage = Usage(HOME, codex_command, start=not args.demo) if preview is None else None
    k = ctypes.WinDLL('kernel32')
    k.GetStdHandle.restype = W.HANDLE
    k.GetConsoleMode.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    k.SetConsoleMode.argtypes = [W.HANDLE, W.DWORD]
    handle = k.GetStdHandle(-11)
    mode = W.DWORD()
    k.GetConsoleMode(handle, ctypes.byref(mode))
    k.SetConsoleMode(handle, mode.value | 4)
    input_handle = k.GetStdHandle(-10)
    input_mode = W.DWORD()
    k.GetConsoleMode(input_handle, ctypes.byref(input_mode))
    k.SetConsoleMode(input_handle, (input_mode.value & ~0x47) | 0x98)
    console = ConsoleInput(k, input_handle)
    k.GetConsoleTitleW.argtypes = [W.LPWSTR, W.DWORD]
    k.SetConsoleTitleW.argtypes = [W.LPCWSTR]
    previous_title = ctypes.create_unicode_buffer(32768)
    k.GetConsoleTitleW(previous_title, len(previous_title))
    k.SetConsoleTitleW('AI Agent Tracker')
    frame = offset = page = 0
    buttons = []
    focus_job = None
    focus_notice = ''
    focus_target = None
    grouping_error = ''
    messaging = MessageGroups()
    try:
        sys.stdout.write(E + '?1049h' + E + '?25l' + E + '2J')
        while True:
            if preview is not None:
                sessions = preview['sessions']
            elif frame % 4 == 0:
                sessions = scan_all(backends) if not args.demo else [dict(name='Claude demo', pid=100+i, directory=r'C:\Projects\example', task=t, activity=a, detail='') for i, (a,t) in enumerate([('reading','Investigate authentication flow'), ('editing','Add settings panel'), ('running','Run test suite'), ('thinking','Plan migration'), ('waiting','Waiting for next prompt')])]
                if args.demo:
                    for i, session in enumerate(sessions):
                        session['provider'] = tuple(PROVIDER_COLORS)[i % len(PROVIDER_COLORS)]
                        session['name'] = session['provider'] + ' demo'
            width, height = shutil.get_terminal_size()
            if focus_job is not None and focus_job.poll() is not None:
                _, error = focus_job.communicate()
                focus_notice = clean(error) if focus_job.returncode else ''
                focus_job = None
                focus_target = None
            if preview is None and not args.demo and frame % 4 == 0:
                try:
                    usage.observe_sessions(sessions)
                    messaging.update(sessions)
                    grouping_error = ''
                except Exception:
                    grouping_error = 'Session usage and message grouping temporarily unavailable'
            notice = preview['notice'] if preview is not None else ' | '.join(filter(None, (
                focus_notice, *setup, *(backend.error for backend, _ in backends),
                grouping_error)))
            snapshot = preview['usage'] if preview is not None else demo_usage() if args.demo else usage.snapshot()
            pages = usage_pages(snapshot)
            page = min(page, max(0, len(pages)-1))
            panels = usage_panels(snapshot, width, page=page)
            output, offset = render(sessions, frame, offset, width, height, notice, panels, buttons,
                                    navigating=focus_target)
            sys.stdout.write(E + 'H' + output)
            sys.stdout.flush()
            for _ in range(5):
                for action in console.actions():
                    if isinstance(action, tuple) and action[0] == 'click':
                        if preview is not None:
                            continue  # Synthetic session IDs must never activate real terminals.
                        _, x, y = action
                        target = next((session for row, start, end, session in buttons
                                       if row == y and start <= x < end), None)
                        if target is not None and focus_job is None:
                            command = [sys.executable]
                            if not getattr(sys, 'frozen', False):
                                command.append(str(Path(__file__).resolve()))
                            env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
                            try:
                                focus_job = subprocess.Popen(command + ['--focus-agent', target['provider'], target['session']],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                                ctypes.WinDLL('user32').AllowSetForegroundWindow(focus_job.pid)
                                focus_target = (target['provider'], target['session'])
                                focus_notice = ''
                            except OSError:
                                focus_notice = 'Could not start terminal navigation'
                    elif action in ('left', 'right'):
                        next_page = min(max(0, page + (1 if action == 'right' else -1)), max(0, len(pages)-1))
                        if next_page != page:
                            page, offset = next_page, 0
                    elif action == 'home':
                        offset = 0
                    elif action == 'end':
                        offset = 10**9
                    else:
                        step = {'up': -1, 'down': 1, 'page_up': -max(1, height-2),
                                'page_down': max(1, height-2)}[action]
                        offset = max(0, offset + step)
                time.sleep(.05)
            frame += 1
    except KeyboardInterrupt:
        pass
    finally:
        if usage is not None:
            usage.stop.set()
        k.SetConsoleTitleW(previous_title.value)
        sys.stdout.write(RESET + E + '?25h' + E + '?1049l')
        sys.stdout.flush()
        k.SetConsoleMode(handle, mode.value)
        k.SetConsoleMode(input_handle, input_mode.value)

if __name__ == '__main__':
    main()
