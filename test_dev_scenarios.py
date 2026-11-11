import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import agent_tracker_dev
import tracker
from dev_scenarios import SCENARIOS, scenario
from usage import usage_pages, usage_panels
from usage_adapters import tile_pages
from usage_tiles import Status


class ScenarioTests(unittest.TestCase):
    def test_setups_have_expected_sessions_and_usage_pages(self):
        expected = {
            1: (2, [['Codex', 'Muse']]), 2: (0, [['Codex', 'Muse']]),
            3: (10, [['Claude', 'Codex'], ['Gemini', 'Grok'], ['Muse']]),
            4: (1, [['Muse']]), 5: (2, [['Codex', 'Muse']]),
            6: (2, [['Codex', 'Muse']]),
            7: (30, [['Claude', 'Codex'], ['Gemini', 'Grok'], ['Muse']]),
            8: (0, []),
        }
        self.assertEqual(set(expected), set(SCENARIOS))
        for number, (count, pages) in expected.items():
            with self.subTest(number=number):
                data = scenario(number, now=1800000000)
                self.assertEqual(len(data['sessions']), count)
                self.assertEqual(usage_pages(data['usage']), pages)
                for width in (40, 80, 160):
                    for page in range(max(1, len(pages))):
                        panels = usage_panels(data['usage'], width, now=1800000000, page=page)
                        tracker.render(data['sessions'], 0, 0, width, 24,
                                       data['notice'], panels)

    def test_missing_allowance_is_distinct_from_loading_and_error(self):
        self.assertEqual(tile_pages(scenario(4)['usage'])[0][0].status, Status.NO_SUBSCRIPTION)
        tiles = tile_pages(scenario(5)['usage'])[0]
        self.assertEqual([tile.status for tile in tiles], [Status.LOADING, Status.UNAVAILABLE])

    def test_fake_json_never_initializes_live_backends(self):
        with contextlib.ExitStack() as stack:
            for name in ('Tracker', 'CodexTracker', 'AntigravityTracker', 'MuseTracker',
                         'GrokTracker', 'Usage', 'configure_usage_collectors'):
                stack.enter_context(patch.object(tracker, name, side_effect=AssertionError(name)))
            for flag, key in (('--json', 'sessions'), ('--usage-json', 'usage')):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch('dev_scenarios.time.time', return_value=1800000000):
                    agent_tracker_dev.main(['--test-1', flag])
                self.assertEqual(json.loads(output.getvalue()), scenario(1, 1800000000)[key])

    def test_release_entry_point_rejects_scenario_flags(self):
        for number in SCENARIOS:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                tracker.main([f'--test-{number}'])
            self.assertEqual(error.exception.code, 2)

    def test_interactive_preview_does_not_touch_live_backends_or_focus(self):
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in ('Tracker', 'CodexTracker', 'AntigravityTracker', 'MuseTracker',
                         'GrokTracker', 'Usage', 'configure_usage_collectors'):
                stack.enter_context(patch.object(tracker, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(tracker.ctypes, 'WinDLL'))
            console = stack.enter_context(patch.object(tracker, 'ConsoleInput'))
            console.return_value.actions.side_effect = [[('click', 23, 20)], KeyboardInterrupt]
            launch = stack.enter_context(patch.object(tracker.subprocess, 'Popen'))
            stack.enter_context(patch.object(tracker.time, 'sleep'))
            stack.enter_context(patch.object(tracker.shutil, 'get_terminal_size', return_value=os.terminal_size((100, 30))))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(patch.object(output, 'isatty', return_value=True))
            agent_tracker_dev.main(['--test-1'])
            launch.assert_not_called()
        self.assertIn('TEST 1:', output.getvalue())
        self.assertIn('2 live sessions', output.getvalue())

    def test_development_requires_exactly_one_scenario(self):
        for args in ([], ['--test-1', '--test-2'], ['--test-1', '--setup-usage']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                agent_tracker_dev.main(args)


if __name__ == '__main__':
    unittest.main()
