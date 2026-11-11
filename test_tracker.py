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
    def test_codex_summary_uses_ephemeral_read_only_run_and_stdin(self):
        def run(args, **kwargs):
            self.assertIn('--ephemeral', args)
            self.assertIn('--ignore-user-config', args)
            self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
            self.assertEqual(kwargs['input'], 'Original task plus refinements')
            self.assertEqual(args[-1], '-')
            self.assertNotIn('shell', kwargs)
            Path(args[args.index('-o') + 1]).write_text('Fix darkroom lighting and door', encoding='utf-8')
            return SimpleNamespace(returncode=0)
        with patch.object(tracker, 'codex_command', return_value=['codex.exe']), patch.object(tracker.subprocess, 'run', side_effect=run):
            self.assertEqual(tracker.generate_summary('Original task plus refinements'), 'Fix darkroom lighting and door')

    def test_claude_cache_is_regenerated_by_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.json'
            context = 'Build camera animations'
            path.write_text(json.dumps({'session': dict(
                digest=tracker.hashlib.sha256(context.encode()).hexdigest(), summary='Old Claude title')}), encoding='utf-8')
            calls = []
            def generate(text):
                calls.append(text)
                return 'Codex title'
            summaries = tracker.Summaries(path, generate)
            summaries.get('session', context, 'Fallback')
            summaries.jobs.join()
            self.assertEqual(calls, [context])
            self.assertEqual(summaries.get('session', context, 'Fallback'), 'Codex title')

    def test_whole_conversation_survives_large_tool_output_and_followup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            rows = [dict(type='user', message=dict(content='Create film camera animations for loading, advancing and focusing.')),
                    dict(type='assistant', message=dict(content='x' * 300000)),
                    dict(type='user', isMeta=True, message=dict(content='Injected instructions')),
                    dict(type='user', message=dict(content=[dict(type='tool_result', content='Ignore this')])),
                    dict(type='user', message=dict(content='Try again to complete the task'))]
            path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
            context, fallback = tracker.conversation_context(path)
            self.assertIn('Create film camera animations', context)
            self.assertIn('Try again', context)
            self.assertNotIn('Injected instructions', context)
            self.assertNotIn('Ignore this', context)
            self.assertTrue(fallback.startswith('Create film camera'))

    def test_saved_title_takes_precedence_over_compaction_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            path.write_text('\n'.join(map(json.dumps, [
                dict(type='custom-title', customTitle='Animate the camera'),
                dict(type='summary', summary='A later compaction summary')])), encoding='utf-8')
            self.assertEqual(tracker.conversation_context(path)[1], 'Animate the camera')

    def test_summaries_cached_across_refreshes_and_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.json'
            calls = []
            def generate(context):
                calls.append(context)
                return 'Fix darkroom lighting, door placement and hand animation'
            summaries = tracker.Summaries(path, generate)
            summaries.get('session', 'Original request', 'Fallback')
            summaries.jobs.join()
            expected = 'Fix darkroom lighting, door placement and hand animation'
            self.assertEqual(summaries.get('session', 'Original request', 'Fallback'), expected)
            restarted = tracker.Summaries(path, generate)
            self.assertEqual(restarted.get('session', 'Original request', 'Fallback'), expected)
            self.assertEqual(calls, ['Original request'])
            restarted.get('session', 'Original request plus followup', 'Fallback')
            restarted.jobs.join()
            self.assertEqual(len(calls), 2)

    def test_failure_keeps_fallback_and_does_not_retry_every_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def fail(context):
                calls.append(context)
                raise RuntimeError('Offline')
            summaries = tracker.Summaries(Path(folder) / 'cache.json', fail)
            summaries.get('session', 'Task', 'Original task')
            summaries.jobs.join()
            self.assertEqual(summaries.get('session', 'Task', 'Original task'), 'Original task')
            self.assertEqual(calls, ['Task'])

    def test_background_result_replaces_fallback_without_transcript_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            project = home / 'projects' / 'example'
            project.mkdir(parents=True)
            (project / 'session.jsonl').write_text(json.dumps(dict(
                type='user', message=dict(content='Fix lighting and the darkroom door.'))), encoding='utf-8')
            summaries = tracker.Summaries(home / 'cache.json', lambda _: 'Fix darkroom lighting and door')
            backend = tracker.Tracker(home, summaries)
            backend.details(dict(sessionId='session'))
            summaries.jobs.join()
            self.assertEqual(backend.details(dict(sessionId='session'))[0], 'Fix darkroom lighting and door')

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
