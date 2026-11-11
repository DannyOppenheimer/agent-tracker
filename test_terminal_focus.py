import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import terminal_focus
import tracker
import usage


class NavigationTests(unittest.TestCase):
    def test_all_five_providers_resolve_to_their_live_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            claude = root / 'claude'
            (claude / 'sessions').mkdir(parents=True)
            (claude / 'sessions' / '123.json').write_text(json.dumps(dict(sessionId='live', pid=123)))
            grok = root / 'grok'
            grok.mkdir()
            (grok / 'active_sessions.json').write_text(json.dumps([dict(session_id='live', pid=456)]))
            muse_lock = root / 'muse/sessions/2026/11/11/live/.session.lock'
            muse_lock.parent.mkdir(parents=True)
            muse_lock.touch()
            with patch.object(tracker, 'HOME', claude), \
                 patch.object(tracker, 'alive', return_value=True), \
                 patch.object(tracker, 'thread_locked', return_value=True), \
                 patch.object(terminal_focus, 'file_owners', return_value=[789]) as owners, \
                 patch.object(terminal_focus, 'focus_console') as focus, \
                 patch.dict(os.environ, GROK_HOME=str(grok), XDG_DATA_HOME=str(root)):
                for provider, pid in [('Claude', 123), ('Grok', 456), ('Codex', 789),
                                      ('Antigravity', 789), ('Muse', 789)]:
                    with self.subTest(provider=provider):
                        terminal_focus.focus_agent(provider, 'live')
                        focus.assert_called_with(pid)
                owners.assert_called_with(muse_lock)

    def test_codex_app_fallback_uses_exact_chat_title(self):
        with patch.object(tracker, 'thread_locked', return_value=True), \
             patch.object(terminal_focus, 'file_owners', return_value=[123]), \
             patch.object(terminal_focus, 'focus_console', side_effect=terminal_focus.NoConsole('app')), \
             patch.object(terminal_focus, 'focus_app_chat') as app, \
             patch.object(tracker.CodexTracker, 'scan', return_value=[dict(session='live', task='Fix camera')]):
            terminal_focus.focus_agent('Codex', 'live')
        app.assert_called_once_with(123, 'Fix camera')

    def test_closed_and_ambiguous_sessions_do_not_focus_another_agent(self):
        with patch.object(tracker, 'thread_locked', return_value=False), \
             patch.object(terminal_focus, 'focus_console') as focus:
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                terminal_focus.focus_agent('Codex', 'closed')
            focus.assert_not_called()
        with patch.object(tracker, 'thread_locked', return_value=True), \
             patch.object(terminal_focus, 'file_owners', return_value=[123, 456]), \
             patch.object(terminal_focus, 'focus_console') as focus:
            with self.assertRaisesRegex(RuntimeError, 'uniquely'):
                terminal_focus.focus_agent('Antigravity', 'live')
            focus.assert_not_called()


if __name__ == '__main__':
    unittest.main()
