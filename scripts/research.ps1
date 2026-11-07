# Thin entrypoint for the single integrated program; it never launches either upstream project.
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$ResearchAppRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:RESEARCH_CRAWLER_HOME = $ResearchAppRoot
$env:PYTHONPATH = Join-Path $ResearchAppRoot 'src'
& $ResearchCrawlerPython -m research_crawler.cli --home $ResearchAppRoot @args
exit $LASTEXITCODE
