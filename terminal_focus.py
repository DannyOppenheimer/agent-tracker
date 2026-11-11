"""Locate an agent's console and select its Windows Terminal tab."""
import base64
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import time
import uuid


class NoConsole(RuntimeError):
    pass


class UniqueProcess(ctypes.Structure):
    _fields_ = [('pid', W.DWORD), ('started', W.FILETIME)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', UniqueProcess), ('name', W.WCHAR * 256),
                ('service', W.WCHAR * 64), ('kind', W.DWORD),
                ('status', W.ULONG), ('session', W.DWORD), ('restartable', W.BOOL)]


class ProcessEntry(ctypes.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                ('heap', ctypes.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                ('executable', W.WCHAR * 260)]


def console_processes(pid):
    """Include an agent frontend when its file-owning worker has no console."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == W.HANDLE(-1).value:
        return [pid]
    processes = {}
    try:
        entry = ProcessEntry(size=ctypes.sizeof(ProcessEntry))
        present = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while present:
            processes[entry.pid] = (entry.parent, entry.executable.lower())
            present = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    result = [pid]
    # Do not walk into IDE/app hosts or unrelated shells above the frontend.
    frontends = {'agy.exe', 'claude.exe', 'codex.exe', 'node.exe',
                 'muse.exe', 'muse-code.exe', 'grok.exe', 'grok-build.exe', 'chatgpt.exe'}
    parent = processes.get(pid, (0, ''))[0]
    while parent not in result and processes.get(parent, (0, ''))[1] in frontends:
        result.append(parent)
        parent = processes[parent][0]
    return result


def file_owners(path):
    """Query Restart Manager for holders of one file. Never stop processes."""
    manager = ctypes.WinDLL('rstrtmgr')
    manager.RmStartSession.argtypes = [ctypes.POINTER(W.DWORD), W.DWORD, W.LPWSTR]
    manager.RmRegisterResources.argtypes = [W.DWORD, W.UINT, ctypes.POINTER(W.LPCWSTR),
                                          W.UINT, ctypes.POINTER(UniqueProcess), W.UINT, ctypes.POINTER(W.LPCWSTR)]
    manager.RmGetList.argtypes = [W.DWORD, ctypes.POINTER(W.UINT), ctypes.POINTER(W.UINT),
                                 ctypes.POINTER(ProcessInfo), ctypes.POINTER(W.DWORD)]
    manager.RmEndSession.argtypes = [W.DWORD]
    session = W.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if manager.RmStartSession(ctypes.byref(session), 0, key):
        raise RuntimeError('Windows could not identify the agent process')
    try:
        files = (W.LPCWSTR * 1)(str(Path(path).resolve()))
        if manager.RmRegisterResources(session, 1, files, 0, None, 0, None):
            raise RuntimeError('Windows could not inspect the agent session')
        needed, count, reason = W.UINT(), W.UINT(), W.DWORD()
        for _ in range(3):
            entries = (ProcessInfo * count.value)()
            status = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reason))
            if status == 0:
                return [entry.process.pid for entry in entries[:count.value]]
            if status != 234:  # ERROR_MORE_DATA
                raise RuntimeError('Windows could not inspect the agent process')
            count.value = needed.value
        raise RuntimeError('Agent processes changed; try again')
    finally:
        manager.RmEndSession(session)


SELECT_TAB = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class AgentTrackerWindow {
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
 [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr window, int command);
}'
$root = [System.Windows.Automation.AutomationElement]::RootElement
$tabCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::TabItem)
for ($attempt = 0; $attempt -lt 8; $attempt++) {
    $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($window in $windows) {
        if ($env:AGENT_TRACKER_APP_PIDS) {
            $owners = $env:AGENT_TRACKER_APP_PIDS | ConvertFrom-Json
            if (($window.Current.ProcessId -notin $owners) -and
                ($window.Current.Name -notmatch '^(Codex|ChatGPT)(\s|$)')) { continue }
            $condition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $env:AGENT_TRACKER_CHAT_TITLE)
            $elements = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
            $targets = @()
            foreach ($element in $elements) {
                $patterns = $element.GetSupportedPatterns()
                if (($patterns -contains [System.Windows.Automation.InvokePattern]::Pattern) -or
                    ($patterns -contains [System.Windows.Automation.SelectionItemPattern]::Pattern)) {
                    $targets += $element
                }
            }
            if ($targets.Count -ne 1) { continue }
            $handle = [IntPtr]$window.Current.NativeWindowHandle
            [void][AgentTrackerWindow]::ShowWindowAsync($handle, 9)
            $patterns = $targets[0].GetSupportedPatterns()
            if ($patterns -contains [System.Windows.Automation.SelectionItemPattern]::Pattern) {
                $targets[0].GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
            } else {
                $targets[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            }
            if (-not [AgentTrackerWindow]::SetForegroundWindow($handle)) { exit 3 }
            exit 0
        }
        if ($window.Current.ClassName -ne 'CASCADIA_HOSTING_WINDOW_CLASS') { continue }
        $tabs = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCondition)
        foreach ($tab in $tabs) {
            if (-not $tab.Current.Name.Contains($env:AGENT_TRACKER_FOCUS_TITLE)) { continue }
            $handle = [IntPtr]$window.Current.NativeWindowHandle
            [void][AgentTrackerWindow]::ShowWindowAsync($handle, 9)
            $selection = $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            $selection.Select()
            if (-not [AgentTrackerWindow]::SetForegroundWindow($handle)) {
                throw 'Windows prevented foreground activation. Check agent and tracker elevation levels.'
            }
            exit 0
        }
    }
    Start-Sleep -Milliseconds 150
}
exit 2
'''


def select_host(environment):
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode(SELECT_TAB.encode('utf-16-le')).decode('ascii')
    with subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                          env=dict(os.environ, **environment), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW) as child:
        ctypes.WinDLL('user32').AllowSetForegroundWindow(child.pid)
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise RuntimeError('Agent window selection timed out')
    return child.returncode == 0


def focus_app_chat(pid, title):
    import json
    if title and select_host(dict(AGENT_TRACKER_APP_PIDS=json.dumps(console_processes(pid)),
                                  AGENT_TRACKER_CHAT_TITLE=title)):
        return
    raise RuntimeError('Could not uniquely select this chat in the app. Keep its sidebar visible and check elevation levels')


def focus_console(pid):
    """Run only in a helper process so attaching cannot detach the dashboard."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.AttachConsole.argtypes = [W.DWORD]
    kernel.GetConsoleTitleW.argtypes = [W.LPWSTR, W.DWORD]
    kernel.SetConsoleTitleW.argtypes = [W.LPCWSTR]
    kernel.GetConsoleWindow.restype = W.HWND
    user.IsWindowVisible.argtypes = [W.HWND]
    user.ShowWindowAsync.argtypes = [W.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [W.HWND]
    kernel.FreeConsole()
    attached = any(kernel.AttachConsole(candidate) for candidate in console_processes(pid))
    if not attached:
        raise NoConsole('This agent has no accessible terminal console (it may be in an app or have closed)')
    previous = ctypes.create_unicode_buffer(32768)
    kernel.GetConsoleTitleW(previous, len(previous))
    changed = False
    try:
        window = kernel.GetConsoleWindow()
        if window and user.IsWindowVisible(window):
            user.ShowWindowAsync(window, 9)
            if user.SetForegroundWindow(window):
                return
            raise RuntimeError('Windows prevented terminal activation; check elevation levels')
        # Give this console a temporary unique title to identify its exact tab.
        marker = 'Agent Tracker ' + uuid.uuid4().hex
        if not kernel.SetConsoleTitleW(marker):
            raise RuntimeError('Could not identify the terminal tab')
        changed = True
        time.sleep(.15)
        if not select_host(dict(AGENT_TRACKER_FOCUS_TITLE=marker)):
            raise RuntimeError('Could not select this tab. Use Windows Terminal with application titles enabled; check elevation levels')
    finally:
        if changed:
            current = ctypes.create_unicode_buffer(32768)
            kernel.GetConsoleTitleW(current, len(current))
            if current.value == marker:
                kernel.SetConsoleTitleW(previous.value)
        kernel.FreeConsole()


def focus_agent(provider, sid):
    """Resolve a current session to its owning process immediately before focus."""
    import json
    from tracker import HOME, CodexTracker, alive, thread_locked
    from usage import antigravity_home
    if not sid or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for character in sid):
        raise RuntimeError('Invalid session identifier')
    if provider in ('Claude', 'Grok'):
        if provider == 'Claude':
            records = []
            for path in (HOME / 'sessions').glob('*.json'):
                try:
                    records.append(json.loads(path.read_text(encoding='utf-8')))
                except (OSError, ValueError):
                    continue
            id_key = 'sessionId'
        else:
            home = Path(os.environ.get('GROK_HOME') or Path.home() / '.grok')
            try:
                records = json.loads((home / 'active_sessions.json').read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                records = []
            if not isinstance(records, list):
                records = []
            id_key = 'session_id'
        for record in records:
            try:
                if isinstance(record, dict) and record.get(id_key) == sid and alive(record):
                    focus_console(int(record['pid']))
                    return
            except (OSError, ValueError, TypeError, KeyError):
                continue
        raise RuntimeError(f'This {provider} session has closed')
    if provider == 'Codex':
        lock = CodexTracker().home / 'thread-writer-locks' / (sid + '.lock')
    elif provider == 'Antigravity':
        lock = antigravity_home() / 'presence' / (sid + '.lock')
    elif provider == 'Muse':
        home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'muse'
        lock = next((home / 'sessions').glob('*/*/*/' + sid + '/.session.lock'), None)
        if lock is None:
            raise RuntimeError('This Muse session has closed')
    else:
        raise RuntimeError('This preview has no terminal')
    if not thread_locked(lock):
        raise RuntimeError('This agent session has closed')
    owners = file_owners(lock)
    if len(owners) != 1:
        raise RuntimeError('Could not uniquely identify the agent terminal process')
    try:
        focus_console(owners[0])
    except NoConsole:
        if provider != 'Codex':
            raise
        session = next((item for item in CodexTracker().scan() if item['session'] == sid), {})
        focus_app_chat(owners[0], session.get('task'))
