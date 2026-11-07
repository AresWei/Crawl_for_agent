"""Download pinned official local-model artifacts; never install globally."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import time
import shutil

ROOT = Path(__file__).resolve().parents[1]
REVISION = "bc640142c66e1fdd12af0bd68f40445458f3869b"
RELEASE = "b12170"
ASSETS = [
    {
        "name": "Qwen3-4B-Q4_K_M.gguf",
        "path": "models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf",
        "url": f"https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/{REVISION}/Qwen3-4B-Q4_K_M.gguf?download=true",
        "bytes": 2497280256,
        "sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
    },
    {
        "name": "llama-b12170-bin-win-cuda-12.4-x64.zip",
        "path": "runtime/model-downloads/llama-b12170-bin-win-cuda-12.4-x64.zip",
        "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/llama-b12170-bin-win-cuda-12.4-x64.zip",
        "bytes": 288725807,
        "sha256": "4c4d2c76ec5d04b7ee5c24219e70c61307737b982b012b4503582b69addf4d8e",
    },
    {
        "name": "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "path": "runtime/model-downloads/cudart-llama-bin-win-cuda-12.4-x64.zip",
        "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/cudart-llama-bin-win-cuda-12.4-x64.zip",
        "bytes": 391443627,
        "sha256": "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
    },
]


def segmented_download(asset: dict, partial: Path) -> None:
    """Keep bounded completed ranges across interruptions; final SHA256 is authoritative."""
    chunks = partial.parent / (asset["name"] + ".chunks")
    chunks.mkdir(exist_ok=True)
    chunk_size = 32 * 1024 * 1024
    if partial.exists() and partial.stat().st_size:
        # Recover complete ranges from an earlier single-stream transfer before
        # any concurrent workers start. Never trust these bytes without final hash.
        with partial.open("rb") as source:
            for index in range(partial.stat().st_size // chunk_size):
                chunk = chunks / f"{index:04d}.bin"
                if chunk.exists() and chunk.stat().st_size == chunk_size:
                    continue
                source.seek(index * chunk_size)
                chunk.write_bytes(source.read(chunk_size))

    def fetch(index: int) -> Path:
        start = index * chunk_size
        end = min(start + chunk_size, asset["bytes"]) - 1
        chunk = chunks / f"{index:04d}.bin"
        if chunk.exists() and chunk.stat().st_size == end - start + 1:
            return chunk
        subprocess.run(
            ["curl.exe", "--fail", "--location", "--retry", "3", "--retry-all-errors", "--retry-delay", "2",
             "--connect-timeout", "30", "--max-time", "180", "--silent", "--show-error",
             "--range", f"{start}-{end}", "--output", str(chunk), asset["url"] + f"&part={index}"],
            check=True,
        )
        if chunk.stat().st_size != end - start + 1:
            raise RuntimeError(f"Server returned an incorrect byte range for chunk {index}")
        print(f"Model range {index + 1}/{(asset['bytes'] + chunk_size - 1) // chunk_size} complete", flush=True)
        return chunk

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ordered = list(pool.map(fetch, range((asset["bytes"] + chunk_size - 1) // chunk_size)))
    with partial.open("wb") as target:
        for chunk in ordered:
            with chunk.open("rb") as source:
                shutil.copyfileobj(source, target, length=asset.get('assembly_buffer_bytes',1024 * 1024))


def download(asset: dict) -> dict:
    path = ROOT / asset["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    started = time.monotonic()
    if not path.exists():
        print(f"Downloading {asset['name']}", flush=True)
        if asset["name"].endswith(".gguf"):
            segmented_download(asset, partial)
        else:
            subprocess.run(
                ["curl.exe", "--fail", "--location", "--retry", "3", "--retry-delay", "2",
                 "--connect-timeout", "30", "--speed-limit", "1024", "--speed-time", "90",
                 "--silent", "--show-error", "--continue-at", "-", "--output", str(partial), asset["url"]],
                check=True,
            )
        candidate = partial
    else:
        candidate = path
    with candidate.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if candidate.stat().st_size != asset["bytes"] or checksum != asset["sha256"]:
        raise RuntimeError(f"Size or SHA-256 verification failed: {candidate.name}")
    if candidate == partial:
        partial.replace(path)
    elapsed = round(time.monotonic() - started, 3)
    print(f"Verified {asset['name']} in {elapsed}s", flush=True)
    return {**asset, "verified_sha256": checksum, "elapsed_seconds": elapsed}


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        verified = list(pool.map(download, ASSETS))
    manifest = {"model_revision": REVISION, "llama_release": RELEASE, "assets": verified}
    (ROOT / "artifacts/model-download-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
