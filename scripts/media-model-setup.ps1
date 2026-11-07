$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$MediaPython = Join-Path $ResearchCrawlerRoot 'runtime/media-model-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $MediaPython)) {
    & $ResearchCrawlerUv venv (Join-Path $ResearchCrawlerRoot 'runtime/media-model-venv') --python $ResearchCrawlerPython
    if ($LASTEXITCODE -ne 0) { throw 'Media worker environment creation failed' }
}
$MediaLock = Join-Path $ResearchCrawlerRoot 'runtime/media-model-lock.txt'
if (Test-Path -LiteralPath $MediaLock) {
    & $ResearchCrawlerUv pip sync --python $MediaPython $MediaLock
} else {
    & $ResearchCrawlerUv pip install --python $MediaPython 'rapidocr==3.9.2' 'onnxruntime==1.31.1' 'faster-whisper==1.2.1' 'pypdfium2==5.15.0' 'psutil==7.2.2' 'Pillow==12.3.0'
}
if ($LASTEXITCODE -ne 0) { throw 'Media worker dependency installation failed' }
& $ResearchCrawlerUv pip check --python $MediaPython
if ($LASTEXITCODE -ne 0) { throw 'Media worker dependency check failed' }
& $ResearchCrawlerPython (Join-Path $PSScriptRoot 'media-model-setup.py')
if ($LASTEXITCODE -ne 0) { throw 'Media model download failed' }
