param([int]$Port = 8946)
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$ResearchAppRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:RESEARCH_CRAWLER_HOME = $ResearchAppRoot
$env:PYTHONPATH = Join-Path $ResearchAppRoot 'src'
& $ResearchCrawlerPython -m research_crawler.cli --home $ResearchAppRoot serve --port $Port
exit $LASTEXITCODE
