[CmdletBinding()]
param([switch]$Cpu, [ValidateRange(5,360)][int]$WaitSeconds = 240,
      [ValidateSet('qwen3-4b','qwen3-8b','qwen3-4b-instruct-2507')][string]$Model = 'qwen3-4b',
      [Parameter(DontShow=$true)][switch]$GpuLeaseHeld)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
if (!$GpuLeaseHeld) {
    $CoordinatedArguments = @((Join-Path $PSScriptRoot 'model-start-coordinated.py'), '--wait-seconds', "$WaitSeconds", '--model', $Model)
    if ($Cpu) { $CoordinatedArguments += '--cpu' }
    & $ResearchCrawlerPython @CoordinatedArguments
    exit $LASTEXITCODE
}
# Only the coordinator's child invocation may enter the existing launcher body.
# The child never reacquires the lock that its Python parent already owns.
$LauncherProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
if (!$env:RESEARCH_CRAWLER_GPU_LAUNCH_OWNER -or
    [string]$LauncherProcess.ParentProcessId -ne $env:RESEARCH_CRAWLER_GPU_LAUNCH_OWNER) {
    throw 'Internal model launcher requires its owning GPU coordinator.'
}
$ModelRuntime = Join-Path $ResearchCrawlerRoot 'runtime/model-server'
$ModelExecutable = Join-Path $ResearchCrawlerRoot 'runtime/model-llama-b12170/llama-server.exe'
$ModelProfileJson = & $ResearchCrawlerPython (Join-Path $PSScriptRoot 'model-profile.py') $Model
if ($LASTEXITCODE -ne 0) { throw 'Selected model profile is not installed and verified.' }
$ModelProfile = $ModelProfileJson | ConvertFrom-Json
$ModelWeights = $ModelProfile.path
$ModelStatePath = Join-Path $ModelRuntime 'state.json'
$ModelTokenPath = Join-Path $ModelRuntime 'token'
if (!(Test-Path -LiteralPath $ModelExecutable) -or !(Test-Path -LiteralPath $ModelWeights)) {
    throw 'Model is not installed. Run scripts/model-setup.ps1 first.'
}
New-Item -ItemType Directory -Force -Path $ModelRuntime | Out-Null
$ModelListeners = @(Get-NetTCPConnection -LocalPort 8947 -State Listen -ErrorAction SilentlyContinue)
if ($ModelListeners.Count -gt 0) {
    if (!(Test-Path -LiteralPath $ModelStatePath)) { throw 'Port 8947 belongs to an unmanaged process.' }
    $ModelPrevious = Get-Content -LiteralPath $ModelStatePath -Raw | ConvertFrom-Json
    $ModelOwner = Get-CimInstance Win32_Process -Filter "ProcessId=$($ModelPrevious.pid)"
    if (!$ModelOwner -or $ModelOwner.ExecutablePath -ne $ModelExecutable -or @($ModelListeners | Where-Object { $_.OwningProcess -ne $ModelPrevious.pid -or $_.LocalAddress -ne '127.0.0.1' }).Count -gt 0) {
        throw 'Port 8947 is occupied by a different process; nothing was stopped.'
    }
    if ($ModelPrevious.model -ne $ModelWeights) { throw 'A different managed model is running; selection must be coordinated.' }
    Write-Output "Local model worker already running (PID $($ModelPrevious.pid), model=$Model)."
    exit 0
}
if (!(Test-Path -LiteralPath $ModelTokenPath)) {
    $ModelRandom = New-Object byte[] 32
    $ModelRandomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $ModelRandomSource.GetBytes($ModelRandom) } finally { $ModelRandomSource.Dispose() }
    [System.IO.File]::WriteAllText($ModelTokenPath, [BitConverter]::ToString($ModelRandom).Replace('-', '').ToLowerInvariant(), [System.Text.UTF8Encoding]::new($false))
}
# Permit only the current account and SYSTEM; the token is not an argument or log entry.
# A fresh descriptor marks only the DACL as modified. Reusing Get-Acl with
# Set-Acl can attempt to persist owner/audit sections and require SeSecurityPrivilege.
$ModelAcl = [System.Security.AccessControl.FileSecurity]::new()
$ModelAcl.SetAccessRuleProtection($true, $false)
$ModelIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ModelAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($ModelIdentity, 'FullControl', 'Allow'))
$ModelAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new('SYSTEM', 'FullControl', 'Allow'))
[System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.FileInfo]::new($ModelTokenPath), $ModelAcl)
$ModelLayers = if ($Cpu) { '0' } else { '99' }
$ModelArguments = @(
    '--model', "`"$ModelWeights`"", '--alias', $ModelProfile.alias,
    '--host', '127.0.0.1', '--port', '8947', '--api-key-file', "`"$ModelTokenPath`"",
    '--ctx-size', '4096', '--parallel', '1', '--n-predict', '1024',
    '--gpu-layers', $ModelLayers, '--threads', '6', '--threads-batch', '6',
    '--batch-size', '256', '--ubatch-size', '128', '--flash-attn', 'on',
    '--reasoning', 'off', '--jinja', '--no-webui', '--no-slots', '--sleep-idle-seconds', '300'
)
$ModelProcess = Start-Process -FilePath $ModelExecutable -ArgumentList $ModelArguments -WorkingDirectory $ModelRuntime -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ModelRuntime 'stdout.log') -RedirectStandardError (Join-Path $ModelRuntime 'stderr.log')
@{ pid=$ModelProcess.Id; executable=$ModelExecutable; model=$ModelWeights; model_id=$ModelProfile.id; alias=$ModelProfile.alias; model_revision=$ModelProfile.revision; model_sha256=$ModelProfile.sha256; host='127.0.0.1'; port=8947; cpu=[bool]$Cpu; started_at=(Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $ModelStatePath -Encoding utf8
$ModelHeaders = @{ Authorization = 'Bearer ' + [System.IO.File]::ReadAllText($ModelTokenPath).Trim() }
$ModelDeadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    if ($ModelProcess.HasExited) { throw "Local model worker exited. See $ModelRuntime/stderr.log" }
    try {
        $ModelRequest = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8947/health')
        $ModelRequest.Proxy = $null
        $ModelRequest.Timeout = 3000
        $ModelRequest.Headers.Add('Authorization', $ModelHeaders.Authorization)
        $ModelResponse = $ModelRequest.GetResponse()
        $ModelReader = [System.IO.StreamReader]::new($ModelResponse.GetResponseStream())
        try { $ModelReady = $ModelReader.ReadToEnd() | ConvertFrom-Json }
        finally { $ModelReader.Dispose(); $ModelResponse.Dispose() }
        if ($ModelReady.status -eq 'ok') {
            Write-Output "Local model ready at http://127.0.0.1:8947 (PID $($ModelProcess.Id), model=$Model, CPU=$([bool]$Cpu))."
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $ModelDeadline)
Write-Warning "Worker PID $($ModelProcess.Id) is still loading. It was left running; inspect runtime/model-server/stderr.log."
exit 2
