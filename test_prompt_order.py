import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import tracker


class PromptOrderTests(unittest.TestCase):
    def test_providers_use_prompt_time_and_ignore_later_output(self):
        cases = {
            'Claude': [dict(type='user', timestamp='2026-11-11T12:00:00Z',
                            message=dict(content='Fix camera')),
                       dict(type='user', timestamp='2026-11-11T13:00:00Z', isMeta=True,
                            message=dict(content='Injected instructions')),
                       dict(type='user', timestamp='2026-11-11T14:00:00Z',
                            message=dict(content=[dict(type='tool_result', content='done')]))],
            'Codex': [dict(type='event_msg', timestamp='2026-11-11T12:00:00Z',
                           payload=dict(type='user_message', message='Fix camera')),
                      dict(type='event_msg', timestamp='2026-11-11T14:00:00Z',
                           payload=dict(type='agent_message', message='Done'))],
            'Muse': [dict(retained_frame='transaction', children=[dict(record_json=json.dumps(dict(
                recorded_at=1794398400000000, payload_type='runtime.session',
                payload=dict(event=dict(kind='started', prompt='Fix camera')))))])],
            'Grok': [dict(timestamp=1794398400000, params=dict(update=dict(
                sessionUpdate='user_message_chunk', content=dict(type='text', text='Fix camera'))))],
        }
        with tempfile.TemporaryDirectory() as folder:
            for provider, rows in cases.items():
                with self.subTest(provider=provider):
                    path = Path(folder) / provider
                    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                    self.assertEqual(tracker.PromptTimes().read(path, provider),
                                     tracker.prompt_timestamp('2026-11-11T12:00:00Z'))

    def test_refresh_preserves_old_prompt_then_promotes_new_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            def append(row):
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row) + '\n')
            append(dict(type='user', timestamp=100, message=dict(content='First prompt')))
            times = tracker.PromptTimes()
            self.assertEqual(times.read(path, 'Claude'), 100)
            append(dict(type='assistant', timestamp=500, message=dict(content='x' * 300000)))
            self.assertEqual(times.read(path, 'Claude'), 100)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('{"type":"user","timestamp":600,')
            self.assertEqual(times.read(path, 'Claude'), 100)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('"message":{"content":"Follow up"}}\n')
            self.assertEqual(times.read(path, 'Claude'), 600)
            path.write_text(json.dumps(dict(type='user', timestamp=700,
                                           message=dict(content='Replaced'))) + '\n', encoding='utf-8')
            self.assertEqual(times.read(path, 'Claude'), 700)

    def test_newest_first_across_directories_and_providers(self):
        sessions = [dict(directory='A', provider='Claude', session='old', last_prompt_at=100),
                    dict(directory='Z', provider='Codex', session='new', last_prompt_at=300),
                    dict(directory='B', provider='Muse', session='middle', last_prompt_at=200),
                    dict(directory='A', provider='Grok', session='unknown')]
        tracker.sort_sessions(sessions)
        self.assertEqual([s['session'] for s in sessions], ['new', 'middle', 'old', 'unknown'])
        sessions[-1]['last_prompt_at'] = 400
        tracker.sort_sessions(sessions)
        self.assertEqual(sessions[0]['session'], 'unknown')

    def test_antigravity_reads_user_input_time(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'presence').mkdir()
            (home / 'presence/live.lock').touch()
            with closing(sqlite3.connect(home / 'conversation_summaries.db')) as db:
                db.execute('CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, '
                           'workspace_uris TEXT, not_fully_idle INTEGER, last_user_input_time TEXT)')
                db.execute('INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)',
                           ('live', 'Camera', '[]', 0, '2026-11-11T12:00:00Z'))
                db.commit()
            with patch.object(tracker, 'thread_locked', return_value=True):
                session, = tracker.AntigravityTracker(home).scan()
            self.assertEqual(session['last_prompt_at'], tracker.prompt_timestamp('2026-11-11T12:00:00Z'))


if __name__ == '__main__':
    unittest.main()
