"""Read account allowances through the agents' own authenticated CLIs.

No prompts, credential files, or billing mutations are involved.
Grok schema: xai-org/grok-build, xai-grok-shell/src/extensions/billing.rs.
Muse schema: Muse Code 1.4.13+, `muse usage --json` (schema v1).
"""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time


def cli_command(name):
    executable = shutil.which(name + '.exe') or shutil.which(name)
    if not executable and name in ('muse', 'grok'):
        candidate = (Path.home() / 'AppData/Local/Programs/muse/muse.cmd' if name == 'muse'
                     else Path(os.environ.get('GROK_HOME') or Path.home() / '.grok') / 'bin/grok.exe')
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        # An already-open terminal inherits PATH from before a new install.
        # Discover the documented Windows install folders without changing PATH.
        folders = [Path.home() / '.local' / 'bin']
        if os.name == 'nt':
            local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
            folders += [local / 'Programs' / name]
            if name == 'grok':
                folders += [Path(os.environ.get('GROK_HOME') or Path.home() / '.grok') / 'bin']
        search_path = os.pathsep.join(str(folder) for folder in folders)
        executable = shutil.which(name + '.exe', path=search_path) or shutil.which(name, path=search_path)
    if not executable:
        return None
    if name == 'muse':
        folder = Path(executable).parent
        try:
            version = (folder / '.muse-version').read_text(encoding='utf-8').strip()
            native = folder / ('muse-bin-' + version + '.exe')
            if re.fullmatch(r'[A-Za-z0-9.-]+', version) and native.is_file():
                return [str(native)]
        except OSError:
            pass
    if Path(executable).suffix.lower() in ('.ps1', '.cmd', '.bat'):
        # Muse's Windows launcher is a PowerShell shim. Quote literals, never
        # splice arguments into shell source; callers append fixed CLI flags.
        return ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                "& '" + executable.replace("'", "''") + "'"]
    return [executable]


def stop_process(process):
    if process.poll() is None:
        if os.name == 'nt':
            # Include children of Windows launchers so a timeout leaves no CLI host.
            subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        if process.poll() is None:
            process.kill()
    process.wait(timeout=5)


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def timestamp(value):
    if number(value) is not None:
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def muse_allowance(payload):
    if not isinstance(payload, dict):
        raise ValueError('Invalid Muse usage response')
    allowance = payload.get('allowance')
    if allowance is None:
        reason = payload.get('reason')
        note = {'api_key': 'API-key login; subscription allowance not reported.',
                'no_allowance': 'Muse reports no subscription allowance for this account.',
                'server_sent_no_usage': 'No subscription allowance reported yet.'}.get(
                    reason, 'Subscription allowance unavailable; check Muse login and subscription.')
        return dict(windows=[], notes=[note], details=[] if reason == 'no_allowance' else [dict(label='', value=note)],
                    billing_mode='token' if reason == 'api_key' else 'unknown',
                    subscription_status='inactive' if reason == 'no_allowance' else 'unknown')
    if not isinstance(allowance, dict):
        raise ValueError('Invalid Muse allowance')
    windows = []
    for key, label in [('window', 'session'), ('weekly', 'week')]:
        bucket = allowance.get(key)
        if not isinstance(bucket, dict):
            continue
        minutes = number(bucket.get('windowDurationMins'))
        if key == 'window' and minutes is not None and minutes > 0:
            label = f'{minutes / 60:g}h' if minutes % 60 == 0 else f'{minutes:g}m'
        reset = number(bucket.get('resetsAtMs'))
        windows.append(dict(label=label, usedPercent=number(bucket.get('usedPercent')),
                            resetsAt=reset / 1000 if reset is not None else None))
    stamp = number(allowance.get('asOfMs'))
    return dict(plan=allowance.get('tier') if isinstance(allowance.get('tier'), str) else '',
                windows=windows, billing_mode='subscription',
                notes=[] if windows else ['Allowance windows not reported.'],
                observed_at=min(stamp / 1000, time.time()) if stamp is not None and stamp > 0 else None)


def muse_usage(command):
    process = subprocess.Popen(command + ['usage', '--json'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        output, _ = process.communicate(timeout=25)
        if process.returncode:
            raise RuntimeError('Muse usage unavailable')
        return muse_allowance(json.loads(output))
    finally:
        stop_process(process)
        process.stdout.close()


def cents(value):
    # The protobuf JSON encoding of zero USD cents is {}.
    return number(value.get('val', 0)) if isinstance(value, dict) else None


def grok_allowance(payload):
    if not isinstance(payload, dict):
        raise ValueError('Invalid Grok billing response')
    config = payload.get('config')
    if not isinstance(config, dict):
        return dict(windows=[], notes=['Account allowance not reported.'])
    period = config.get('currentPeriod') or {}
    if not isinstance(period, dict):
        period = {}
    label = {'USAGE_PERIOD_TYPE_WEEKLY': 'week', 'USAGE_PERIOD_TYPE_MONTHLY': 'month'}.get(
        period.get('type'), 'allowance')
    percent = number(config.get('creditUsagePercent'))
    used, limit = cents(config.get('used')), cents(config.get('monthlyLimit'))
    if percent is None and used is not None and limit is not None and limit > 0:
        percent = used / limit * 100
        label = 'month'
    window = dict(label=label, usedPercent=percent,
                  resetsAt=timestamp(period.get('end') or config.get('billingPeriodEnd')))
    notes, details = [], []
    for key, title in [('prepaidBalance', 'Prepaid credits'), ('onDemandUsed', 'On-demand used'),
                       ('onDemandCap', 'On-demand cap')]:
        value = cents(config.get(key))
        if value is not None:
            notes.append(f'{title}: ${value / 100:.2f}')
            details.append(dict(label=title, value=f'${value / 100:.2f}'))
    return dict(plan=payload.get('subscription_tier') if isinstance(payload.get('subscription_tier'), str) else '',
                windows=[window], notes=notes, details=details, billing_mode='subscription')


def grok_usage(command):
    process = subprocess.Popen(command + ['agent', 'stdio'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        messages.put(message)
                except ValueError:
                    pass
        finally:
            messages.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def request(identity, method, params):
        process.stdin.write(json.dumps(dict(jsonrpc='2.0', id=identity, method=method, params=params)) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 25
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Grok billing timed out')
            message = messages.get(timeout=remaining)
            if message is None:
                raise RuntimeError('Grok billing host exited')
            if message.get('id') == identity:
                return message
            if 'id' in message and 'method' in message:
                # Never grant tool/terminal access to a quota-only client.
                process.stdin.write(json.dumps(dict(jsonrpc='2.0', id=message['id'],
                    error=dict(code=-32601, message='Unsupported by usage reader'))) + '\n')
                process.stdin.flush()
    try:
        initialized = request(1, 'initialize', dict(protocolVersion=1, clientCapabilities={},
            clientInfo=dict(name='agent-tracker', version='1.0'),
            _meta=dict(startupHints=dict(nonInteractive=True))))
        if 'error' in initialized:
            raise RuntimeError('Grok initialization failed')
        # ACP extension method names carry an underscore on the wire.
        response = request(2, '_x.ai/billing', {})
        if 'error' in response:
            raise RuntimeError('Grok billing unavailable')
        return grok_allowance(response.get('result'))
    finally:
        stop_process(process)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
