param([switch]$Enable, [switch]$LoginStart)
$ErrorActionPreference = 'Stop'
$pluginSource = Join-Path $PSScriptRoot 'codex-warmup'
$pluginPython = (Get-Command python -ErrorAction Stop).Source
$pluginCodex = Get-Command codex.exe -ErrorAction SilentlyContinue
if (-not $pluginCodex) {
    $pluginCodex = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin') -Filter codex.exe -Recurse |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $pluginCodex) { throw 'Codex CLI not found; no automatic download/login performed.' }
    $pluginCodexPath = $pluginCodex.FullName
} else { $pluginCodexPath = $pluginCodex.Source }
& $pluginPython -B -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python prerequisite failed.' }
& $pluginCodexPath plugin marketplace add $PSScriptRoot --json
if ($LASTEXITCODE -ne 0) { throw 'Marketplace registration failed.' }
& $pluginCodexPath plugin add codex-warmup@codex-warmup-local --json
if ($LASTEXITCODE -ne 0) { throw 'Plugin installation failed.' }
if ($LoginStart) {
    & $pluginPython -B (Join-Path $pluginSource 'server.py') autostart-on
    if ($LASTEXITCODE -ne 0) { throw 'Login startup setup failed.' }
}
if ($Enable) {
    & $pluginPython -B (Join-Path $pluginSource 'server.py') enable
    if ($LASTEXITCODE -ne 0) { throw 'Warm-up enable failed; inspect state before retrying.' }
}
& $pluginPython -B (Join-Path $pluginSource 'server.py') status
