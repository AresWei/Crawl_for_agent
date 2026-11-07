$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$SemanticPython = Join-Path $ResearchCrawlerRoot 'runtime/semantic-model-venv/Scripts/python.exe'
$SemanticLock = Join-Path $ResearchCrawlerRoot 'runtime/semantic-model-lock.txt'
if (-not (Test-Path -LiteralPath $SemanticPython)) {
    & $ResearchCrawlerUv venv (Join-Path $ResearchCrawlerRoot 'runtime/semantic-model-venv') --python $ResearchCrawlerPython
    if ($LASTEXITCODE -ne 0) { throw 'Semantic environment creation failed' }
}
& $ResearchCrawlerUv pip install --python $SemanticPython 'https://download-r2.pytorch.org/whl/cpu/torch-2.9.0%2Bcpu-cp312-cp312-win_amd64.whl' 'transformers==4.57.6' 'onnxruntime==1.24.4' 'scipy==1.17.0' 'nltk==3.9.2' 'psutil>=6.1.1'
if ($LASTEXITCODE -ne 0) { throw 'Semantic dependency installation failed' }
& $ResearchCrawlerUv pip check --python $SemanticPython
if ($LASTEXITCODE -ne 0) { throw 'Semantic dependency compatibility check failed' }
& $ResearchCrawlerUv pip freeze --python $SemanticPython | Set-Content -Encoding utf8 $SemanticLock
& $SemanticPython (Join-Path $PSScriptRoot 'semantic-setup.py')
if ($LASTEXITCODE -ne 0) { throw 'Semantic model setup failed' }
& $SemanticPython (Join-Path $PSScriptRoot 'semantic-topic-setup.py')
if ($LASTEXITCODE -ne 0) { throw 'Semantic topic model setup failed' }
