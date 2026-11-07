# Foreground auxiliary component of Research Crawler; no installation or auto-start.
param([int]$Port = 8950)
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$SigningProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:RESEARCH_CRAWLER_HOME = $SigningProjectRoot
$env:PYTHONPATH = Join-Path $SigningProjectRoot 'src'
& $ResearchCrawlerPython (Join-Path $PSScriptRoot 'signing-service.py') --root $SigningProjectRoot --port $Port
exit $LASTEXITCODE
