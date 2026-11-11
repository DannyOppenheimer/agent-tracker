import json
import msvcrt
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import tracker


class SummaryTests(unittest.TestCase):
    def test_infinite_timestamps_do_not_hang(self):
        from message_groups import stamp
        self.assertEqual(tracker.prompt_timestamp('1e999'), 0)
        self.assertEqual(tracker.prompt_timestamp(float('inf')), 0)
        self.assertEqual(stamp(float('inf')), 0)
        self.assertEqual(tracker.prompt_timestamp(1_700_000_000_000), 1_700_000_000)

    def test_scanner_failure_becomes_notice_not_crash(self):
        class Broken:
            error = ''
            def scan(self):
                raise AttributeError('unexpected row')
        class Working:
            error = ''
            def scan(self):
                return [dict(provider='Codex', directory='C:/x', session='s')]
        broken = Broken()
        sessions = tracker.scan_all([(broken, 'Claude'), (Working(), 'Codex')])
        self.assertEqual([s['session'] for s in sessions], ['s'])
        self.assertIn('Claude', broken.error)

    def test_grok_setup_never_writes_invalid_toml(self):
        import tempfile, tomllib
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / 'config.toml'
            config.write_text('ui = { theme = "dark" }\n', encoding='utf-8')
            with patch.dict(tracker.os.environ, GROK_HOME=folder), \
                 patch.object(tracker, 'HOME', home / 'missing-claude'), \
                 patch.object(tracker, 'antigravity_home', return_value=home / 'missing-agy'), \
                 patch.object(tracker.shutil, 'which', return_value=None):
                messages = tracker.configure_usage_collectors()
            self.assertEqual(config.read_text(encoding='utf-8'), 'ui = { theme = "dark" }\n')
            self.assertTrue(any('manually' in m for m in messages))
            config.write_text('[ui]\ntheme = "dark"\n', encoding='utf-8')
            with patch.dict(tracker.os.environ, GROK_HOME=folder), \
                 patch.object(tracker, 'HOME', home / 'missing-claude'), \
                 patch.object(tracker, 'antigravity_home', return_value=home / 'missing-agy'), \
                 patch.object(tracker.shutil, 'which', return_value=None):
                tracker.configure_usage_collectors()
            data = tomllib.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(data['ui']['theme'], 'dark')
            self.assertEqual(data['ui']['status_line']['type'], 'command')

    def test_title_priority_rename_then_claude_ai_title_then_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            rows = [dict(type='user', message=dict(content='This is a Unity project. Please fix the darkroom door.')),
                    dict(type='assistant', message=dict(content='x' * 300000)),
                    dict(type='user', isMeta=True, message=dict(content='Injected instructions')),
                    dict(type='user', message=dict(content=[dict(type='tool_result', content='Ignore this')]))]
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n', encoding='utf-8')
            titles = tracker.TranscriptTitles()
            self.assertEqual(titles.read(path), 'Fix the darkroom door')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='summary', summary='Compaction summary')) + '\n')
                stream.write(json.dumps(dict(type='ai-title', aiTitle='Darkroom door')) + '\n')
            self.assertEqual(titles.read(path), 'Darkroom door')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='ai-title', aiTitle='Darkroom door and lighting')) + '\n')
            self.assertEqual(titles.read(path), 'Darkroom door and lighting')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(type='custom-title', customTitle='Animate the camera')) + '\n')
                stream.write(json.dumps(dict(type='ai-title', aiTitle='A later AI title')) + '\n')
            self.assertEqual(titles.read(path), 'Animate the camera')

    def test_title_waits_for_unfinished_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            path.write_text('{"type": "ai-title", "aiTi', encoding='utf-8')
            titles = tracker.TranscriptTitles()
            self.assertEqual(titles.read(path), 'Task not available')
            path.write_text('{"type": "ai-title", "aiTitle": "Finished"}', encoding='utf-8')
            self.assertEqual(titles.read(path), 'Finished')

    def test_claude_session_uses_transcript_title(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            project = home / 'projects' / 'example'
            project.mkdir(parents=True)
            (project / 'session.jsonl').write_text('\n'.join(map(json.dumps, [
                dict(type='user', message=dict(content='Fix lighting and the darkroom door.')),
                dict(type='ai-title', aiTitle='Darkroom lighting and door')])) + '\n', encoding='utf-8')
            backend = tracker.Tracker(home)
            self.assertEqual(backend.details(dict(sessionId='session'))[0], 'Darkroom lighting and door')

    def test_task_wraps_into_spare_line(self):
        session = dict(provider='Claude', name='Example', pid=1, session='test', directory='Example',
                       task='Fix darkroom lighting and door placement', activity='waiting', detail='')
        output, _ = tracker.render([session], 0, 0, 60, 24)
        self.assertIn('Task: Fix darkroom lighting and', output)
        self.assertIn('\x1b[0mplacement\x1b[K', output)


class TerminalButtonTests(unittest.TestCase):
    def test_buttons_track_visible_rows_after_scrolling(self):
        session = dict(provider='Claude', name='Example', pid=1, session='test', directory='Example',
                       task='Task', activity='waiting', detail='')
        buttons = []
        output, _ = tracker.render([session]*4, 0, 0, 80, 12, buttons=buttons)
        self.assertIn('[Go to terminal]', output)
        self.assertEqual(buttons[0][:3], (6, 22, 38))
        tracker.render([session]*4, 0, 1, 80, 12, buttons=buttons)
        self.assertEqual(buttons[0][:3], (5, 22, 38))
        tracker.render([session]*4, 0, 7, 80, 12, buttons=buttons)
        self.assertTrue(all(row >= 0 for row, _, _, _ in buttons))
        self.assertEqual(len(buttons), 1)
        tracker.render([session], 0, 0, 30, 20, buttons=buttons)
        self.assertEqual(buttons, [])


class CodexTests(unittest.TestCase):
    def test_only_held_locks_are_live(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'thread.lock'
            with path.open('w+b') as owner:
                self.assertFalse(tracker.thread_locked(path))
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    self.assertTrue(tracker.thread_locked(path))
                finally:
                    owner.seek(0)
                    msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
                self.assertFalse(tracker.thread_locked(path))

    def test_transcript_activity_and_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            backend = tracker.CodexTracker(folder)
            events = [dict(type='event_msg', payload=dict(type='task_started')),
                      dict(type='response_item', payload=dict(type='function_call', name='exec_command', arguments=json.dumps({'cmd': 'rg example .'})))]
            path.write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
            self.assertEqual(backend.details(path)[0], 'reading')
            events.append(dict(type='response_item', payload=dict(type='custom_tool_call', name='apply_patch', input='*** Begin Patch')))
            path.write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
            self.assertEqual(backend.details(path)[0], 'editing')
            events.append(dict(type='event_msg', payload=dict(type='task_complete')))
            path.write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
            self.assertEqual(backend.details(path), ('waiting', ''))

    def test_scan_excludes_saved_but_closed_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            locks = home / 'thread-writer-locks'
            locks.mkdir()
            with sqlite3.connect(home / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)')
                db.execute('INSERT INTO threads VALUES (?, ?, ?, ?)', ('live', str(home / 'missing.jsonl'), r'C:\Projects', 'Build a dashboard'))
                db.execute('INSERT INTO threads VALUES (?, ?, ?, ?)', ('closed', str(home / 'old.jsonl'), r'C:\Projects', 'Old task'))
            db.close()
            (locks / 'closed.lock').touch()
            with (locks / 'live.lock').open('w+b') as owner:
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    sessions = tracker.CodexTracker(home).scan()
                    self.assertEqual([s['session'] for s in sessions], ['live'])
                    self.assertEqual(sessions[0]['provider'], 'Codex')
                finally:
                    owner.seek(0)
                    msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == '__main__':
    unittest.main()
