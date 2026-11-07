[CmdletBinding()]
param([switch]$Start)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
& $ResearchCrawlerPython (Join-Path $PSScriptRoot 'model-download.py')
if ($LASTEXITCODE -ne 0) { throw 'Model artifact download or verification failed.' }
$ModelBinaryDirectory = Join-Path $ResearchCrawlerRoot 'runtime/model-llama-b12170'
New-Item -ItemType Directory -Force -Path $ModelBinaryDirectory | Out-Null
foreach ($ModelArchive in @('llama-b12170-bin-win-cuda-12.4-x64.zip', 'cudart-llama-bin-win-cuda-12.4-x64.zip')) {
    Expand-Archive -LiteralPath (Join-Path $ResearchCrawlerRoot "runtime/model-downloads/$ModelArchive") -DestinationPath $ModelBinaryDirectory -Force
}
$ModelSources = @{
    'runtime/model-server/LICENSE.llama.cpp' = 'https://raw.githubusercontent.com/ggml-org/llama.cpp/b12170/LICENSE'
    'models/qwen3-4b/LICENSE' = 'https://huggingface.co/Qwen/Qwen3-4B-GGUF/raw/bc640142c66e1fdd12af0bd68f40445458f3869b/LICENSE'
    'models/qwen3-4b/README.upstream.md' = 'https://huggingface.co/Qwen/Qwen3-4B-GGUF/raw/bc640142c66e1fdd12af0bd68f40445458f3869b/README.md'
}
New-Item -ItemType Directory -Force -Path (Join-Path $ResearchCrawlerRoot 'runtime/model-server') | Out-Null
foreach ($ModelSource in $ModelSources.GetEnumerator()) {
    Invoke-WebRequest -Uri $ModelSource.Value -OutFile (Join-Path $ResearchCrawlerRoot $ModelSource.Key)
}
& (Join-Path $ModelBinaryDirectory 'llama-server.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'The local model executable could not load.' }
if ($Start) { & (Join-Path $PSScriptRoot 'model-start.ps1') }
