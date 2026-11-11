import json
from pathlib import Path
import tempfile
import unittest

from message_groups import MessageGroups, session_blocks
from usage_tiles import ANSI
import tracker


def session(provider, sid, path=None):
    return dict(provider=provider, session=sid, name=sid, pid=None, message_log=str(path) if path else None,
                directory='Project', task='Work on task', activity='waiting', detail='')


def write(path, rows):
    with path.open('a', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row) + '\n')


def prompt(t, text='Human prompt'):
    return dict(type='user', uuid='prompt-' + str(t), timestamp=t, message=dict(content=text))


def send(peer, t, failed=False):
    return [dict(type='assistant', timestamp=t, message=dict(content=[dict(
        type='tool_use', id='call-' + str(t), name='SendMessage', input=dict(recipient=peer, content='Hello'))])),
        dict(type='user', timestamp=t+.1, message=dict(content=[dict(type='tool_result',
            tool_use_id='call-' + str(t), is_error=failed, content='Error: refused' if failed else 'Message sent successfully')]))]


class MessagingTests(unittest.TestCase):
    def test_three_prompts_remove_only_that_member_and_new_exchange_rejoins(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / (sid + '.jsonl') for sid in ('a', 'b', 'c')]
            a, b, c = [session('Claude', sid, path) for sid, path in zip(('a', 'b', 'c'), paths)]
            for path in paths:
                path.touch()
            write(paths[0], [prompt(1)] + send('b', 2))
            write(paths[1], send('c', 3))
            groups = MessageGroups()
            groups.update([a, b, c])
            self.assertEqual(len({s['message_group'] for s in (a, b, c)}), 1)
            write(paths[0], [prompt(4), prompt(5)])
            groups.update([a, b, c])
            self.assertIn('message_group', a)
            # Refreshes and tool output are not new human prompts.
            groups.update([a, b, c])
            write(paths[0], [dict(type='user', isMeta=True, timestamp=5.5, message=dict(content='Injected')),
                              prompt(6)])
            groups.update([a, b, c])
            self.assertNotIn('message_group', a)
            self.assertEqual(b['message_group'], c['message_group'])
            write(paths[0], send('b', 7))
            groups.update([a, b, c])
            self.assertEqual(a['message_group'], b['message_group'])
            # Replay on restart gives the same membership and prompt expiry.
            MessageGroups().update([a, b, c])
            self.assertEqual(a['message_group'], b['message_group'])

    def test_failed_sends_ambiguous_names_and_idle_subscriptions_do_not_group(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.jsonl'
            a, b, c = session('Claude', 'a', path), session('Claude', 'b'), session('Claude', 'c')
            b['name'] = c['name'] = 'Worker'
            write(path, send('b', 1, failed=True) + send('Worker', 2))
            write(path, [dict(type='assistant', timestamp=3, message=dict(content=[dict(
                type='tool_use', id='idle', name='SendMessage', input=dict(to='b', notify_when_idle=True))])),
                dict(type='user', timestamp=4, message=dict(content=[dict(type='tool_result', tool_use_id='idle', content='Subscribed')]))])
            groups = MessageGroups()
            groups.update([a, b, c])
            self.assertTrue(all('message_group' not in s for s in (a, b, c)))

    def test_received_peer_message_is_not_a_human_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'b.jsonl'
            a, b = session('Claude', 'a'), session('Claude', 'b', path)
            write(path, [prompt(1, '<teammate-message teammate_id="a">Hello</teammate-message>'), prompt(2), prompt(3)])
            groups = MessageGroups()
            groups.update([a, b])
            self.assertIn('message_group', b)
            write(path, [prompt(4)])
            groups.update([a, b])
            self.assertNotIn('message_group', b)
            self.assertNotIn('message_group', a)

    def test_codex_sends_require_successful_output_and_live_recipient(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.jsonl'
            a, b = session('Codex', 'a', path), session('Codex', 'b')
            write(path, [dict(type='response_item', timestamp=1, payload=dict(type='function_call',
                call_id='call', name='send_input', arguments=json.dumps(dict(id='b', message='Review'))))])
            groups = MessageGroups()
            groups.update([a, b])
            self.assertNotIn('message_group', a)
            # Retry an incomplete final log write once its newline is present.
            output = dict(type='response_item', timestamp=2, payload=dict(type='function_call_output',
                call_id='call', output=json.dumps(dict(submission_id='accepted'))))
            with path.open('a') as stream:
                stream.write(json.dumps(output))
            groups.update([a, b])
            self.assertNotIn('message_group', a)
            with path.open('a') as stream:
                stream.write('\n')
            groups.update([a, b])
            self.assertEqual(a['message_group'], b['message_group'])
            groups.update([a])
            self.assertNotIn('message_group', a)

    def test_muse_peer_observations_deduplicate_and_expire(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'b.jsonl'
            path.touch()
            a, b = session('Muse', 'a'), session('Muse', 'b', path)
            b['usage'] = dict(peer_messages=[])
            groups = MessageGroups()
            groups.update([a, b])
            b['usage']['peer_messages'] = [dict(id='cursor-1', sender='a')]
            groups.update([a, b])
            self.assertEqual(a['message_group'], b['message_group'])
            write(path, [dict(recorded_at=i, payload_type='runtime.session',
                             payload=dict(kind='run', event=dict(kind='started', prompt='Human input')))
                         for i in (1, 2, 3)])
            groups.update([a, b])
            self.assertNotIn('message_group', a)
            self.assertNotIn('message_group', b)

    def test_grok_confirmed_message_and_prompt_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'grok.jsonl'
            a, b = session('Grok', 'a', path), session('Grok', 'b')
            def update(t, **fields):
                return dict(timestamp=t, params=dict(update=fields))
            write(path, [update(1, sessionUpdate='tool_call', toolCallId='send', status='in_progress',
                        name='send_subagent_message', rawInput=dict(subagent_id='b', message='Review')),
                         update(2, sessionUpdate='tool_call_update', toolCallId='send', status='completed',
                                rawOutput=dict(success=True))])
            groups = MessageGroups()
            groups.update([a, b])
            self.assertEqual(a['message_group'], b['message_group'])
            write(path, [update(3, sessionUpdate='user_message_chunk', content=dict(type='text', text='First half')),
                         update(3.1, sessionUpdate='user_message_chunk', content=dict(type='text', text='Second half'))])
            groups.update([a, b])
            self.assertEqual(groups.counts[('Grok', 'a')], 1)
            self.assertIn('message_group', a)

    def test_navigation_status_stays_in_the_clicked_button_while_scrolled(self):
        sessions = [session('Claude', str(i)) for i in range(8)]
        sessions[4]['message_group'] = sessions[5]['message_group'] = 'group'
        for width in (60, 120):
            output, offset = tracker.render(sessions, 0, 35, width, 30,
                                            navigating=('Claude', '4'))
            plain = ANSI.sub('', output)
            self.assertGreater(offset, 0)
            self.assertIn('[Navigating to terminal,', plain)
            self.assertIn('wait...]', plain)
            self.assertNotIn('Switching to agent terminal', plain)
        output, _ = tracker.render(sessions, 0, 35, 120, 30)
        self.assertNotIn('Navigating', output)


    def test_group_border_and_button_coordinates_survive_scrolling(self):
        a, b, c = [session('Claude', sid) for sid in ('a', 'b', 'c')]
        a['message_group'] = c['message_group'] = 'group'
        self.assertEqual(list(session_blocks([a, b, c])), [[a, c], [b]])
        for width in (40, 80, 120):
            for offset in (0, 4, 9):
                buttons = []
                output, _ = tracker.render([a, b, c], 0, offset, width, 80, buttons=buttons)
                lines = [ANSI.sub('', row) for row in output.split('\r\n')]
                self.assertIn('Exchanged messages', '\n'.join(lines))
                self.assertIn('╭', '\n'.join(lines))
                self.assertIn('╰', '\n'.join(lines))
                for row, start, end, member in buttons:
                    self.assertIn('[Go to terminal]', lines[row])
                    self.assertEqual(lines[row][start:end], '[Go to terminal]')
                    self.assertEqual(start, 25 if member in (a, c) else 22)


if __name__ == '__main__':
    unittest.main()
