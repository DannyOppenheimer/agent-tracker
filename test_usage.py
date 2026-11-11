import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from usage import Usage, capture_claude, usage_lines, usage_panels, ANSI, RED
import tracker


class UsageTests(unittest.TestCase):
    def test_interactive_main_renders_usage_before_exit(self):
        from unittest.mock import patch, MagicMock
        snapshot = dict(Claude={}, Codex=dict(data={}), Gemini={})
        output = MagicMock()
        output.isatty.return_value = True
        with patch.object(tracker.sys, 'argv', ['agent-tracker']), \
             patch.object(tracker.sys, 'stdout', output), \
             patch.object(tracker, 'configure_usage_collectors', return_value=[]), \
             patch.object(tracker.ctypes, 'WinDLL'), \
             patch.object(tracker, 'Tracker') as claude, \
             patch.object(tracker, 'CodexTracker') as codex, \
             patch.object(tracker, 'AntigravityTracker') as gemini, \
             patch.object(tracker, 'Usage') as usage, \
             patch.object(tracker, 'ConsoleInput') as console:
            for backend in (claude.return_value, codex.return_value, gemini.return_value):
                backend.scan.return_value = []
                backend.error = ''
            claude.return_value.summaries.error = ''
            usage.return_value.snapshot.return_value = snapshot
            console.return_value.actions.side_effect = KeyboardInterrupt
            tracker.main()
        written = ''.join(call.args[0] for call in output.write.call_args_list)
        self.assertIn('No active agent chats.', written)
        usage.return_value.stop.set.assert_called_once()

    def test_antigravity_collection_and_page(self):
        from usage import capture_antigravity, antigravity_snapshot, gemini_lines
        with tempfile.TemporaryDirectory() as home, contextlib.redirect_stdout(io.StringIO()):
            capture_antigravity(dict(session_id='one', quota={
                'gemini-weekly': dict(remaining_fraction=.25, reset_time='2030-01-01T00:00:00Z'),
                'disabled': dict(disabled=True),
                'credits': dict(remaining_amount=42)}, secret='excluded'), home)
            capture_antigravity(dict(session_id='two', model=dict(display_name='Gemini')), home)
            snapshot = dict(Gemini=antigravity_snapshot(home))
            text = '\n'.join(gemini_lines(snapshot, now=1000))
            self.assertIn('75% used (25% left)', text)
            self.assertIn('disabled: disabled', text)
            self.assertIn('42 remaining', text)
            for path in (Path(home) / 'tracker-usage').glob('*.json'):
                self.assertNotIn('secret', json.loads(path.read_text()))
            for width in (60, 80, 110, 160):
                lines = usage_panels(snapshot, width, now=1000, page=1)
                plain = '\n'.join(ANSI.sub('', line) for line in lines)
                self.assertIn('GEMINI / USAGE', plain)
                self.assertNotIn('CLAUDE / USAGE', plain)
                self.assertLessEqual(max(len(ANSI.sub('', line)) for line in lines), width)
            main = '\n'.join(usage_panels(snapshot, 120, now=1000, page=0))
            self.assertIn('GEMINI / USAGE', main)
            self.assertNotIn('CLAUDE / USAGE', main)
            snapshot['Claude'] = dict(model=dict(display_name='Sonnet'))
            main = '\n'.join(usage_panels(snapshot, 120, now=1000))
            self.assertIn('CLAUDE / USAGE', main)
            self.assertIn('GEMINI / USAGE', main)
            self.assertEqual(usage_panels(dict(Codex=dict(error='CLI unavailable')), 120), [])

    def test_panels_fit_terminal_and_highlight_full_quota(self):
        snapshot = dict(Claude=dict(rate_limits=dict(five_hour=dict(
            used_percentage=100, resets_at=4600))), Codex=dict(data=dict(
            rateLimitResetCredits=dict(availableCount=2))))
        for width in (60, 80, 110, 120, 160):
            lines = usage_panels(snapshot, width, now=1000)
            plain = '\n'.join(ANSI.sub('', line) for line in lines)
            self.assertLessEqual(max(len(ANSI.sub('', line)) for line in lines), width)
            self.assertIn('100% used', plain)
            self.assertIn('RESETS LEFT: 2', plain)
            self.assertIn(RED, '\n'.join(lines))
            self.assertIn('█', plain)
            self.assertNotIn('[###', plain)

    def test_scroll_moves_whole_dashboard_exactly_one_line(self):
        session = dict(provider='Codex', name='Example', session='id', pid=None,
                       directory='Project', task='Task', activity='waiting', detail='')
        usage = ['usage ' + str(i) for i in range(6)]
        first, start = tracker.render([session]*4, 0, 0, 120, 20, usage=usage)
        second, offset = tracker.render([session]*4, 0, 1, 120, 20, usage=usage)
        self.assertEqual(offset, start+1)
        # Compare viewport lines excluding the clear-to-end command on the last row.
        self.assertEqual(first.split('\r\n')[1:-1], second.split('\r\n')[:-2])
        last, end = tracker.render([session]*4, 0, 10**9, 120, 20, usage=usage)
        self.assertLess(end, 10**9)
        self.assertNotIn('Showing ', last)

    def test_capture_excludes_chat_and_keeps_last_quota_across_new_sessions(self):
        with tempfile.TemporaryDirectory() as home, contextlib.redirect_stdout(io.StringIO()):
            capture_claude(dict(session_id='one', rate_limits=dict(five_hour=dict(
                used_percentage=25, resets_at=5000)), secret='do not persist'), home)
            capture_claude(dict(session_id='two', model=dict(display_name='Opus')), home)
            usage = Usage(home, None, start=False)
            snapshot = usage.snapshot()['Claude']
            self.assertEqual(snapshot['rate_limits']['five_hour']['used_percentage'], 25)
            self.assertEqual(snapshot['model']['display_name'], 'Opus')
            for path in (Path(home) / 'tracker-usage').glob('*.json'):
                self.assertNotIn('secret', json.loads(path.read_text()))

    def test_actual_window_durations_and_reset_credits(self):
        data = dict(rateLimits=dict(primary=dict(usedPercent=0, windowDurationMins=300,
            resetsAt=4600), secondary=dict(usedPercent=44, windowDurationMins=10080, resetsAt=500)),
            rateLimitResetCredits=dict(availableCount=2, credits=[dict(status='available',
                title='Full reset', expiresAt=10000)]))
        text = '\n'.join(usage_lines(dict(Codex=dict(data=data, updated_at=900)), now=1000))
        self.assertIn('5h [----------] 0% used (100% left)', text)
        self.assertIn('week [####------] 44% used', text)
        self.assertIn('awaiting refresh', text)
        self.assertIn('resets left: 2', text)
        self.assertIn('Full reset | expires', text)
        self.assertNotIn('reset credits: not exposed', text)

    def test_nonstandard_window_is_not_falsely_labelled_five_hour(self):
        text = '\n'.join(usage_lines(dict(Codex=dict(data=dict(rateLimits=dict(
            primary=dict(usedPercent=10, windowDurationMins=15)))), now=1000)))
        self.assertIn('15m [#---------]', text)
        self.assertNotIn('5h [', text)

    def test_failed_refresh_keeps_old_data_and_age(self):
        text = '\n'.join(usage_lines(dict(Codex=dict(updated_at=1, error='refresh failed',
            data=dict(rateLimitResetCredits=dict(availableCount=0)))), now=3601))
        self.assertIn('updated 1h 0m ago', text)
        self.assertIn('resets left: 0', text)
        self.assertIn('refresh failed', text)


if __name__ == '__main__':
    unittest.main()
