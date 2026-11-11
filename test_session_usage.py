import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from session_usage import CodexSessionUsage, muse_snapshot_metrics, read_muse_session
from usage import Usage, capture_claude, capture_antigravity, usage_lines
import tracker


def event(inp, cached, out, last=None):
    tokens = dict(input_tokens=inp, cached_input_tokens=cached, output_tokens=out,
                  total_tokens=inp + out)
    return dict(type='event_msg', payload=dict(type='token_count', info=dict(
        total_token_usage=tokens, last_token_usage=last or tokens, model_context_window=10000)))


class SessionUsageTests(unittest.TestCase):
    def test_muse_protocol_reads_latest_cumulative_metrics_without_a_prompt(self):
        server = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'initialized':
        continue
    result = {}
    if method == 'initialize':
        pass
    elif method == 'session/read':
        assert request['params']['excludeItems'] is True
        result = {'history': {'mode': 'none'}}
    elif method == 'view/page':
        assert request['params']['direction'] == 'backward'
        if 'cursor' not in request['params']:
            result = {'events': [
                {'method': 'session/tokenUsage', 'params': {'cumulative': {'totalTokens': 10, 'cost': {'usd': 1, 'partial': False}}}},
                {'method': 'session/tokenUsage', 'params': {'cumulative': {'totalTokens': 20, 'cost': {'usd': 2, 'partial': False}}}}
            ], 'nextCursor': 'older'}
        else:
            result = {'events': [
                {'method': 'session/contextUsage', 'params': {'usedTokens': 100, 'windowTokens': 1000}}
            ], 'nextCursor': None}
    else:
        raise AssertionError('Unexpected or mutating method: ' + method)
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''
        self.assertEqual(read_muse_session([sys.executable, '-u', '-c', server], 'one'),
                         dict(total_tokens=20, cost_usd=2, cost_partial=False, context_percent=10, peer_messages=[]))

    def test_muse_uses_server_cumulative_cost_and_context(self):
        result = dict(history=dict(snapshot=dict(state=dict(
            tokenUsage=dict(totalTokens=9000, cost=dict(usd=2.3, partial=True)),
            contextUsage=dict(usedTokens=500, windowTokens=1000)))))
        self.assertEqual(muse_snapshot_metrics(result), dict(total_tokens=9000, cost_usd=2.3,
                                                           cost_partial=True, context_percent=50))
        self.assertEqual(muse_snapshot_metrics({}), {})

    def test_sessions_keep_their_own_reported_metrics_without_spend_history(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            homes = {'Claude': root / 'claude', 'Antigravity': root / 'gemini'}
            capture_claude(dict(session_id='one', context_window=dict(used_percentage=12),
                                cost=dict(total_cost_usd=2)), homes['Claude'])
            capture_claude(dict(session_id='two', context_window=dict(used_percentage=65),
                                cost=dict(total_cost_usd=3)), homes['Claude'])
            capture_antigravity(dict(conversation_id='three', context_window=dict(used_percentage=40),
                                     cost=dict(total_cost_usd=4)), homes['Antigravity'])
            usage = Usage(homes['Claude'], None, start=False)
            sessions = [dict(provider='Claude', session='one'), dict(provider='Claude', session='two'),
                        dict(provider='Antigravity', session='three'), dict(provider='Muse', session='four')]
            usage.observe_sessions(sessions, homes)
            self.assertEqual([s['usage'].get('context_percent') for s in sessions], [12, 65, 40, None])
            for _ in range(3):
                usage.observe_sessions(sessions, homes)
            self.assertNotIn('Spend', usage.snapshot())
            self.assertFalse((homes['Claude'] / 'tracker-spend.json').exists())
            self.assertEqual([s['usage'].get('cost_usd') for s in sessions], [2, 3, 4, None])
            shared = '\n'.join(usage_lines(usage.snapshot()))
            self.assertNotIn('session context', shared)
            self.assertNotIn('session API-equivalent', shared)

    def test_codex_cached_tokens_duplicates_model_switches_and_partial_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            rows = [dict(type='turn_context', payload=dict(model='gpt-6.1-sol')), event(1000, 800, 100)]
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n', encoding='utf-8')
            reader = CodexSessionUsage()
            first = reader.read(path)
            self.assertNotIn('cost_usd', first)
            self.assertEqual(first['context_percent'], 11)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(rows[-1]) + '\n')
                stream.write(json.dumps(dict(type='turn_context', payload=dict(model='gpt-6-luna'))) + '\n')
                last = dict(input_tokens=1000, cached_input_tokens=0, output_tokens=100, total_tokens=1100)
                stream.write(json.dumps(event(2000, 800, 200, last)) + '\n')
                stream.write('{"type":')
            second = reader.read(path)
            self.assertNotIn('cost_usd', second)
            self.assertEqual(second['total_tokens'], 2200)
            self.assertEqual(reader.read(path), second)
            # An incomplete final row must be read again after completion.
            with path.open('a', encoding='utf-8') as stream:
                stream.write('"turn_context","payload":{"model":"unknown"}}\n')
                stream.write(json.dumps(event(3000, 800, 300, last)) + '\n')
            third = reader.read(path)
            self.assertNotIn('cost_usd', third)
            self.assertEqual(third['total_tokens'], 3300)

    def test_codex_token_counts_do_not_generate_costs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            rows = [dict(type='turn_context', payload=dict(model='gpt-6.1-sol')), event(300000, 200000, 1000)]
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n', encoding='utf-8')
            self.assertNotIn('cost_usd', CodexSessionUsage().read(path))
            path.write_text(json.dumps(event(1000, 0, 100)) + '\n', encoding='utf-8')
            metrics = CodexSessionUsage().read(path)
            self.assertNotIn('cost_usd', metrics)
            self.assertEqual(metrics['total_tokens'], 1100)

    def test_per_agent_rendering_and_buttons_after_extra_rows(self):
        sessions = [dict(provider='Claude', name='Example', session=sid, pid=123,
                         directory='Project', task='Task', activity='waiting', detail='',
                         usage=dict(context_percent=percent, cost_usd=cost))
                    for sid, percent, cost in [('one', 12, 2), ('two', 65, 3)]]
        buttons = []
        output, _ = tracker.render(sessions, 0, 0, 80, 80, buttons=buttons)
        self.assertIn('Context 12.0%', output)
        self.assertIn('Context 65.0%', output)
        self.assertIn('Reported session cost: $2.0000', output)
        lines = output.split('\r\n')
        for row, _, _, session in buttons:
            self.assertIn('[Go to terminal]', lines[row])

    def test_grok_setup_preserves_existing_status_line(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.object(tracker, 'HOME', home / 'claude'), \
                 patch.object(tracker, 'antigravity_home', return_value=home / 'gemini'), \
                 patch.object(tracker.shutil, 'which', return_value=None), \
                 patch.dict(tracker.os.environ, {'GROK_HOME': str(home / 'grok')}):
                grok = home / 'grok'
                grok.mkdir()
                config = grok / 'config.toml'
                config.write_text('[ui]\ncolor = true\n', encoding='utf-8')
                tracker.configure_usage_collectors()
                saved = config.read_text(encoding='utf-8')
                self.assertIn('--capture-grok-usage', saved)
                tracker.configure_usage_collectors()
                self.assertEqual(config.read_text(encoding='utf-8'), saved)


if __name__ == '__main__':
    unittest.main()
