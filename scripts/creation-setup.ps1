$ErrorActionPreference = 'Stop'
$creationRoot = Split-Path -Parent $PSScriptRoot
$env:TEMP = Join-Path $creationRoot 'temp'
$env:TMP = $env:TEMP
& (Join-Path $creationRoot 'runtime/venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'creation-setup.py')
if ($LASTEXITCODE -ne 0) { throw 'Local creation setup failed' }
