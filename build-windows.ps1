param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $architecture = & $Python -c "import struct, sysconfig; print('x86' if struct.calcsize('P') == 4 else 'arm64' if 'arm64' in sysconfig.get_platform() else 'x64')"
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the Python architecture.' }

    & $Python -m PyInstaller --noconfirm --onedir --console --name agent-tracker `
        --distpath .build/dist --workpath .build/work --specpath .build tracker.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    Copy-Item -LiteralPath WINDOWS-QUICKSTART.txt -Destination .build/dist/agent-tracker
    New-Item -ItemType Directory -Path release -Force | Out-Null
    $archive = "release/agent-tracker-windows-$architecture.zip"
    Compress-Archive -Path .build/dist/agent-tracker -DestinationPath $archive -Force
    Write-Output (Resolve-Path $archive)
} finally {
    Pop-Location
}
