import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import quota
import usage


class AllowanceTests(unittest.TestCase):
    def test_muse_percentages_and_millisecond_resets(self):
        result = quota.muse_allowance(dict(schemaVersion=1, allowance=dict(
            tier='Everyday', asOfMs=1000000,
            window=dict(usedPercent=25, resetsAtMs=4600000, windowDurationMins=300),
            weekly=dict(usedPercent=80, resetsAtMs=9000000)), secret='excluded'))
        self.assertEqual(result['windows'][0], dict(label='5h', usedPercent=25, resetsAt=4600))
        self.assertEqual(result['windows'][1]['usedPercent'], 80)
        self.assertEqual(result['observed_at'], 1000)
        self.assertNotIn('secret', result)

    def test_muse_unknown_window_duration_and_no_subscription(self):
        result = quota.muse_allowance(dict(allowance=dict(window=dict(usedPercent=0))))
        self.assertEqual(result['windows'][0]['label'], 'session')
        self.assertIsNone(result['windows'][0]['resetsAt'])
        result = quota.muse_allowance(dict(allowance=None, reason='api_key'))
        self.assertEqual(result['windows'], [])
        self.assertIn('API-key', result['notes'][0])

    def test_grok_weekly_pool_and_credit_cents(self):
        result = quota.grok_allowance(dict(subscription_tier='SuperGrok', config=dict(
            creditUsagePercent=42.5, currentPeriod=dict(type='USAGE_PERIOD_TYPE_WEEKLY',
            end='2030-01-01T00:00:00Z'), prepaidBalance=dict(val=1250),
            onDemandUsed={}, onDemandCap=dict(val=5000)), secret='excluded'))
        self.assertEqual(result['windows'][0]['label'], 'week')
        self.assertEqual(result['windows'][0]['usedPercent'], 42.5)
        self.assertEqual(result['windows'][0]['resetsAt'], 1893456000)
        self.assertIn('Prepaid credits: $12.50', result['notes'])
        self.assertIn('On-demand used: $0.00', result['notes'])
        self.assertNotIn('secret', result)

    def test_grok_legacy_monthly_and_missing_limits(self):
        result = quota.grok_allowance(dict(config=dict(used=dict(val=500), monthlyLimit=dict(val=2000))))
        self.assertEqual(result['windows'][0]['label'], 'month')
        self.assertEqual(result['windows'][0]['usedPercent'], 25)
        result = quota.grok_allowance(dict(config=dict(used={}, monthlyLimit={})))
        self.assertIsNone(result['windows'][0]['usedPercent'])
        self.assertEqual(quota.grok_allowance(dict(config=None))['windows'], [])

    def test_nonfinite_and_boolean_readings_are_unavailable(self):
        for value in (True, '25', float('nan'), float('inf')):
            result = quota.muse_allowance(dict(allowance=dict(window=dict(usedPercent=value))))
            self.assertIsNone(result['windows'][0]['usedPercent'])


class CollectorTests(unittest.TestCase):
    def test_new_windows_installs_are_found_with_stale_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            grok = root / '.grok' / 'bin' / 'grok.exe'
            muse = root / 'Local' / 'Programs' / 'muse' / 'muse.cmd'
            for executable in (grok, muse):
                executable.parent.mkdir(parents=True)
                executable.touch()
            with patch.object(quota.Path, 'home', return_value=root), patch.dict(
                    quota.os.environ, {'PATH': '', 'LOCALAPPDATA': str(root / 'Local'),
                                       'GROK_HOME': str(root / '.grok')}):
                self.assertEqual(Path(quota.cli_command('grok')[0]), grok)
                self.assertIn(str(muse).lower(), quota.cli_command('muse')[-1].lower())

    def test_muse_runs_only_usage_json(self):
        script = "import json,sys; assert sys.argv[1:]==['usage','--json']; print(json.dumps({'allowance':{'window':{'usedPercent':12}}}))"
        result = quota.muse_usage([sys.executable, '-c', script])
        self.assertEqual(result['windows'][0]['usedPercent'], 12)

    def test_grok_read_only_protocol_and_notifications(self):
        script = '''import json,sys
def send(value):
    print(json.dumps(value), flush=True)
init=json.loads(sys.stdin.readline())
assert init['method']=='initialize'
assert init['params']['clientCapabilities']=={}
send({'jsonrpc':'2.0','id':init['id'],'result':{'protocolVersion':1}})
request=json.loads(sys.stdin.readline())
assert request['method']=='_x.ai/billing'
assert request['params']=={}
send({'jsonrpc':'2.0','method':'notification','params':{}})
send({'jsonrpc':'2.0','id':request['id'],'result':{'config':{'creditUsagePercent':33}}})
'''
        result = quota.grok_usage([sys.executable, '-c', script])
        self.assertEqual(result['windows'][0]['usedPercent'], 33)

    def test_protocol_error_never_leaks_server_details(self):
        script = '''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'id':request['id'],'error':{'code':-32000,'message':'secret-value'}}),flush=True)
'''
        with self.assertRaisesRegex(RuntimeError, '^Grok initialization failed$'):
            quota.grok_usage([sys.executable, '-c', script])

    def test_refresh_failure_keeps_previous_data_and_time(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(usage, 'cli_command', return_value=['muse.exe']):
            backend = usage.Usage(folder, lambda: ['codex.exe'], start=False)
            expected = dict(windows=[dict(label='week', usedPercent=25, resetsAt=5000)])
            with patch.object(usage, 'muse_usage', return_value=expected):
                backend.refresh('Muse')
            before = backend.snapshot()['Muse']
            with patch.object(usage, 'muse_usage', side_effect=RuntimeError('secret-value')):
                backend.refresh('Muse')
            after = backend.snapshot()['Muse']
            self.assertEqual(after['data'], expected)
            self.assertEqual(after['updated_at'], before['updated_at'])
            self.assertIn('refresh failed', after['error'])
            self.assertNotIn('secret-value', json.dumps(after))


class CardTests(unittest.TestCase):
    def test_pages_colors_and_width(self):
        snapshot = dict(Claude=dict(model={}), Codex=dict(data=dict(rateLimits={})), Gemini=dict(quota={}))
        for name in ('Muse', 'Grok'):
            snapshot[name] = dict(data=dict(windows=[dict(label='week', usedPercent=25, resetsAt=4600)]),
                                  updated_at=900, detected=True)
        self.assertEqual(usage.usage_pages(snapshot), [['Claude', 'Codex'], ['Gemini', 'Grok'], ['Muse']])
        for width in (40, 60, 80, 110, 160):
            lines = usage.usage_panels(snapshot, width, now=1000, page=1) + usage.usage_panels(snapshot, width, now=1000, page=2)
            plain = '\n'.join(usage.ANSI.sub('', line) for line in lines)
            self.assertIn('MUSE / USAGE', plain)
            self.assertIn('GROK / USAGE', plain)
            self.assertIn('25% used', plain)
            self.assertIn('75% left', plain)
            self.assertIn('38;2;255;153;204m', '\n'.join(lines))
            self.assertIn('38;2;170;170;170m', '\n'.join(lines))
            self.assertLessEqual(max(len(usage.ANSI.sub('', line)) for line in lines), width)

    def test_missing_quotas_and_refresh_failure_remain_visible(self):
        snapshot = dict(Muse=dict(detected=True, data={}, error='refresh failed', updated_at=0))
        plain = '\n'.join(usage.ANSI.sub('', line) for line in usage.usage_panels(snapshot, 80))
        self.assertIn('MUSE / USAGE', plain)
        self.assertIn('Usage unavailable', plain)
        self.assertIn('refresh failed', plain)
        self.assertNotIn('0% used', plain)
        self.assertEqual(usage.usage_pages(dict(Muse=dict(detected=False, data={}))), [])


if __name__ == '__main__':
    unittest.main()
