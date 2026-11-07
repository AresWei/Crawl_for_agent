# Dot-source this file before using the project runtime. All changes are process scoped.
$ResearchCrawlerRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ResearchCrawlerDirectories = @{
    TEMP = 'temp'; TMP = 'temp'
    UV_CACHE_DIR = 'cache/uv'; UV_PYTHON_CACHE_DIR = 'cache/python'
    UV_PYTHON_INSTALL_DIR = 'runtime/python'; UV_PYTHON_BIN_DIR = 'runtime/bin'
    UV_TOOL_DIR = 'runtime/uv-tools'; UV_TOOL_BIN_DIR = 'runtime/bin'
    UV_CREDENTIALS_DIR = 'runtime/uv-credentials'; PIP_CACHE_DIR = 'cache/pip'
    PLAYWRIGHT_BROWSERS_PATH = 'runtime/browsers'; XDG_CACHE_HOME = 'cache'
    HF_HOME = 'cache/huggingface'; HF_HUB_CACHE = 'cache/huggingface/hub'
    HF_ASSETS_CACHE = 'cache/huggingface/assets'; CUDA_CACHE_PATH = 'cache/cuda'
    TORCH_HOME = 'cache/torch'; NLTK_DATA = 'cache/nltk'
    TORCHINDUCTOR_CACHE_DIR = 'cache/torchinductor'; TRITON_CACHE_DIR = 'cache/triton'
    MPLCONFIGDIR = 'cache/matplotlib'; NUMBA_CACHE_DIR = 'cache/numba'
}
foreach ($ResearchCrawlerEntry in $ResearchCrawlerDirectories.GetEnumerator()) {
    $ResearchCrawlerPath = Join-Path $ResearchCrawlerRoot $ResearchCrawlerEntry.Value
    New-Item -ItemType Directory -Path $ResearchCrawlerPath -Force | Out-Null
    [Environment]::SetEnvironmentVariable($ResearchCrawlerEntry.Key, $ResearchCrawlerPath, 'Process')
}
$env:UV_NO_MODIFY_PATH = '1'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_NO_CONFIG = '1'
$env:UV_NO_HF_TOKEN = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:UV_CONCURRENT_DOWNLOADS = '4'
$env:UV_CONCURRENT_BUILDS = '2'
$ResearchCrawlerPython = Join-Path $ResearchCrawlerRoot 'runtime/venv/Scripts/python.exe'
$ResearchCrawlerUv = Join-Path $ResearchCrawlerRoot 'runtime/uv/uv.exe'
