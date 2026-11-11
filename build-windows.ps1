param([string]$Python = 'python', [switch]$Development)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $architecture = & $Python -c "import struct, sysconfig; print('x86' if struct.calcsize('P') == 4 else 'arm64' if 'arm64' in sysconfig.get_platform() else 'x64')"
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the Python architecture.' }

    $entryPoint = if ($Development) { 'agent_tracker_dev.py' } else { 'tracker.py' }
    $buildRoot = if ($Development) { '.build/development' } else { '.build' }
    $exclusions = if ($Development) { @() } else { @('--exclude-module', 'agent_tracker_dev', '--exclude-module', 'dev_scenarios') }
    & $Python -m PyInstaller --noconfirm --onedir --console --name agent-tracker `
        --distpath "$buildRoot/dist" --workpath "$buildRoot/work" --specpath $buildRoot @exclusions $entryPoint
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    if ($Development) {
        Write-Output (Resolve-Path "$buildRoot/dist/agent-tracker/agent-tracker.exe")
        return
    }

    Copy-Item -LiteralPath WINDOWS-QUICKSTART.txt -Destination .build/dist/agent-tracker
    New-Item -ItemType Directory -Path release -Force | Out-Null
    $archive = "release/agent-tracker-windows-$architecture.zip"
    Compress-Archive -Path .build/dist/agent-tracker -DestinationPath $archive -Force
    Write-Output (Resolve-Path $archive)
} finally {
    Pop-Location
}
