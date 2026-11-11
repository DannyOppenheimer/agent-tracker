"""Session fixtures for Muse Code and the official Grok Build CLI."""
import json
import msvcrt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tracker


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(map(json.dumps, rows)) + '\n', encoding='utf-8')


def muse_event(kind, **fields):
    return dict(payload_type='runtime.session', payload=dict(
        kind='run', event=dict(kind=kind, **fields)))


def grok_event(update_kind, **fields):
    return dict(jsonrpc='2.0', method='session/update', params=dict(
        sessionId='live', update=dict(sessionUpdate=update_kind, **fields)))


class MuseTests(unittest.TestCase):
    def test_current_metadata_and_retained_transaction_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            metadata = dict(payload_type='runtime.session.metadata', payload=dict(
                kind='metadata', record=dict(workspace_root=r'C:\Project')))
            prompt = muse_event('started', prompt='Fix the camera')
            frame = dict(retained_frame='session_permission_transaction', children=[
                dict(child_index=0, record_json=json.dumps(prompt)),
                dict(child_index=1, record_json='{incomplete')])
            write_rows(path, [metadata, frame])
            directory, task, state, detail = tracker.MuseTracker(folder).details(path)
            self.assertEqual(directory, r'C:\Project')
            self.assertEqual(task, 'Fix the camera')
            self.assertEqual(state, 'thinking')

    def test_held_lock_is_live_and_closed_history_is_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'sessions/2026/11/11/live/session.jsonl'
            rows = [dict(payload_type='runtime.session.metadata', payload=dict(workspace_root=r'C:\Project')),
                    muse_event('started', prompt='Fix the camera'),
                    muse_event('assistant_tool_calls_committed', tool_calls=[dict(
                        function=dict(name='edit_file', arguments=json.dumps(dict(path='camera.py'))))])]
            write_rows(path, rows)
            lock = path.with_name('.session.lock')
            lock.write_bytes(b'pid=123\n')
            backend = tracker.MuseTracker(root)
            backend.session_usage = Mock()
            backend.session_usage.get.return_value = {}
            self.assertEqual(backend.scan(), [])
            with lock.open('r+b') as owner:
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    session, = backend.scan()
                    self.assertEqual(session['provider'], 'Muse')
                    self.assertEqual(session['directory'], r'C:\Project')
                    self.assertEqual(session['activity'], 'editing')
                    self.assertIn('camera.py', session['detail'])
                    self.assertEqual(session['task'], 'Fix the camera')
                finally:
                    owner.seek(0)
                    msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
            self.assertEqual(backend.scan(), [])

    def test_completion_large_output_and_followups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            rows = [muse_event('started', prompt='Original objective'),
                    muse_event('assistant_message_committed', text='x' * 300000)]
            rows += [muse_event('started', prompt=f'Refinement {i}') for i in range(8)]
            rows += [muse_event('terminal')]
            write_rows(path, rows)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('{"incomplete":')
            _, task, state, detail = tracker.MuseTracker(folder).details(path)
            self.assertEqual(task, 'Original objective')
            self.assertEqual((state, detail), ('waiting', ''))

    def test_title_skips_context_and_greeting_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            write_rows(path, [muse_event('started', prompt='hi'),
                              muse_event('started', prompt='This repo is a Unity game. Please fix the camera jitter.')])
            _, task, _, _ = tracker.MuseTracker(folder).details(path)
            self.assertEqual(task, 'Fix the camera jitter')

    def test_xdg_home(self):
        with patch.dict(tracker.os.environ, {'XDG_DATA_HOME': r'C:\AgentData'}):
            self.assertEqual(tracker.MuseTracker().home, Path(r'C:\AgentData') / 'muse')


class GrokTests(unittest.TestCase):
    def test_registry_excludes_dead_processes_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entries = [dict(session_id='live', pid=123, cwd=r'C:\Project'),
                       dict(session_id='dead', pid=456, cwd=r'C:\Project'), None]
            (root / 'active_sessions.json').write_text(json.dumps(entries), encoding='utf-8')
            path = root / 'sessions' / tracker.quote(r'C:\Project', safe='') / 'live'
            write_rows(path / 'updates.jsonl', [grok_event('tool_call', kind='read',
                                                         title='Read camera.py', status='in_progress')])
            (path / 'summary.json').write_text(json.dumps(dict(generated_title='Fix camera movement')),
                                             encoding='utf-8')
            with patch.object(tracker, 'alive', side_effect=lambda row: row['pid'] == 123):
                session, = tracker.GrokTracker(root).scan()
            self.assertEqual(session['provider'], 'Grok')
            self.assertEqual(session['task'], 'Fix camera movement')
            self.assertEqual(session['activity'], 'reading')
            self.assertEqual(session['detail'], 'Read camera.py')
            self.assertEqual(session['pid'], 123)

    def test_activity_completion_and_partial_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'updates.jsonl'
            backend = tracker.GrokTracker(folder)
            rows = [grok_event('user_message_chunk', content=dict(type='text', text='Add camera controls')),
                    grok_event('tool_call', kind='execute', status='in_progress', title='Run tests')]
            write_rows(path, rows)
            self.assertEqual(backend.details(path), ('running', 'Run tests', 'Add camera controls'))
            rows += [grok_event('turn_completed')]
            write_rows(path, rows)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('{')
            self.assertEqual(backend.details(path), ('waiting', '', 'Add camera controls'))

    def test_missing_transcript_and_invalid_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backend = tracker.GrokTracker(root)
            self.assertEqual(backend.scan(), [])
            (root / 'active_sessions.json').write_text(json.dumps([
                dict(session_id='new', pid=123, cwd=r'C:\Project')]), encoding='utf-8')
            with patch.object(tracker, 'alive', return_value=True):
                session, = backend.scan()
                self.assertEqual(session['task'], 'Task not available')
            (root / 'active_sessions.json').write_text('{}', encoding='utf-8')
            self.assertEqual(backend.scan(), [])
            self.assertIn('registry', backend.error)


class ProviderRenderingTests(unittest.TestCase):
    def test_new_provider_colors(self):
        for provider, color in [('Muse', tracker.PINK), ('Grok', tracker.LIGHT_GRAY)]:
            session = dict(provider=provider, name=provider, session='live', pid=None,
                           directory='Project', task='Fix camera', activity='waiting', detail='')
            output, _ = tracker.render([session], 0, 0, 120, 24)
            self.assertIn(color + provider.upper(), output)
            self.assertIn(color + tracker.mascot('waiting', 0)[1], output)


if __name__ == '__main__':
    unittest.main()
