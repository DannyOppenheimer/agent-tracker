import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import terminal_focus
import tracker
import usage


class NavigationTests(unittest.TestCase):
    def test_codex_tab_label_matches_session_folder_and_optional_spinner(self):
        pattern = terminal_focus.codex_tab_pattern('Fix agent (v2).', r'C:\Users\oppen\claude-tracker')
        for label in ['Fix agent (v2). | claude-tracker', '\u283c Fix agent (v2). | claude-tracker']:
            self.assertRegex(label, pattern)
        for label in ['Fix agent (v2). | other-folder', 'Other Fix agent (v2). | claude-tracker',
                      'Fix agent v2x | claude-tracker', 'Fix agent (v2). | claude-tracker extra']:
            self.assertIsNone(re.search(pattern, label))

    def test_shared_codex_server_selects_terminal_by_session_label(self):
        with patch.object(tracker, 'thread_locked', return_value=True), \
             patch.object(terminal_focus, 'file_owners', return_value=[123]), \
             patch.object(terminal_focus, 'focus_console', side_effect=terminal_focus.NoConsole('daemon')), \
             patch.object(tracker.CodexTracker, 'scan', return_value=[
                 dict(session='live', task='Fix camera', directory=r'C:\work\game')]), \
             patch.object(terminal_focus, 'select_host', return_value=True) as select, \
             patch.object(terminal_focus, 'focus_app_chat') as app:
            terminal_focus.focus_agent('Codex', 'live')
        select.assert_called_once_with(dict(
            AGENT_TRACKER_CODEX_TAB_PATTERN=terminal_focus.codex_tab_pattern('Fix camera', r'C:\work\game')))
        app.assert_not_called()

    def test_classic_console_restores_only_when_minimized(self):
        for minimized in [False, True]:
            with self.subTest(minimized=minimized):
                kernel, user = MagicMock(), MagicMock()
                kernel.AttachConsole.return_value = True
                kernel.GetConsoleWindow.return_value = 123
                user.IsWindowVisible.return_value = True
                user.IsIconic.return_value = minimized
                def get_class(handle, buffer, size):
                    buffer.value = 'ConsoleWindowClass'
                    return len(buffer.value)
                user.GetClassNameW.side_effect = get_class
                with patch.object(terminal_focus.ctypes, 'WinDLL', side_effect=[kernel, user]), \
                     patch.object(terminal_focus, 'console_processes', return_value=[42]):
                    terminal_focus.focus_console(42)
                if minimized:
                    user.ShowWindowAsync.assert_called_once_with(123, 9)
                else:
                    user.ShowWindowAsync.assert_not_called()
                user.SetForegroundWindow.assert_called_once_with(123)

    def test_visible_pseudoconsole_selects_tab_instead_of_only_raising_window(self):
        kernel, user = MagicMock(), MagicMock()
        kernel.AttachConsole.return_value = True
        kernel.GetConsoleWindow.return_value = 123
        user.IsWindowVisible.return_value = True
        title = ['original agent title']
        def get_title(buffer, size):
            buffer.value = title[0]
            return len(title[0])
        def set_title(value):
            title[0] = value
            return True
        def get_class(handle, buffer, size):
            buffer.value = 'PseudoConsoleWindow'
            return len(buffer.value)
        kernel.GetConsoleTitleW.side_effect = get_title
        kernel.SetConsoleTitleW.side_effect = set_title
        user.GetClassNameW.side_effect = get_class
        with patch.object(terminal_focus.ctypes, 'WinDLL', side_effect=[kernel, user]), \
             patch.object(terminal_focus, 'console_processes', return_value=[42]), \
             patch.object(terminal_focus.time, 'sleep'), \
             patch.object(terminal_focus, 'select_host', return_value=True) as select:
            terminal_focus.focus_console(42)
        select.assert_called_once()
        self.assertIn('AGENT_TRACKER_FOCUS_TITLE', select.call_args.args[0])
        user.SetForegroundWindow.assert_not_called()
        self.assertEqual(title[0], 'original agent title')
        kernel.FreeConsole.assert_called()

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
