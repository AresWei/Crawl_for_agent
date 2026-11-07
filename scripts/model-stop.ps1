$ErrorActionPreference = 'Stop'
$ModelProject = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ModelStatePath = Join-Path $ModelProject 'runtime/model-server/state.json'
if (!(Test-Path -LiteralPath $ModelStatePath)) { Write-Output 'No managed model worker state.'; exit 0 }
$ModelState = Get-Content -LiteralPath $ModelStatePath -Raw | ConvertFrom-Json
$ModelProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($ModelState.pid)"
if (!$ModelProcess) { Write-Output 'Managed model worker is already stopped.'; exit 0 }
$ModelExpected = Join-Path $ModelProject 'runtime/model-llama-b12170/llama-server.exe'
if ($ModelProcess.ExecutablePath -ne $ModelExpected -or $ModelProcess.CommandLine -notlike '*--port 8947*') {
    throw 'PID no longer matches the managed model executable and port; nothing was stopped.'
}
Stop-Process -Id $ModelState.pid
Write-Output "Stopped managed model worker PID $($ModelState.pid)."
