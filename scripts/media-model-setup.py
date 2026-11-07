"""Fetch only pinned public local-media weights; all writes stay in the project."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = "536b0662742c02347bc0e980a01041f333bce120"
OCR_REVISION = "v3.9.2"
OCR = [
    ("det", "ch_PP-OCRv5_det_mobile.onnx", "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae"),
    ("rec", "ch_PP-OCRv5_rec_mobile.onnx", "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5"),
    ("cls", "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx", "54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7"),
]
ASR_HASHES = {
    "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
    "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
    "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    "README.md": "329373481008c7c38654aff8ecdcf0163c211557cc7ba8e2ef6f2f84b4f75ec8",
}


def download(relative, url, expected=None):
    destination = ROOT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and (not expected or hashlib.file_digest(destination.open("rb"), "sha256").hexdigest() == expected):
        pass
    else:
        partial = destination.with_suffix(destination.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        actual = hashlib.file_digest(partial.open("rb"), "sha256").hexdigest()
        if expected and actual != expected:
            raise ValueError(f"Model checksum mismatch: {destination.name}")
        partial.replace(destination)
    with destination.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    return {"path": str(relative), "url": url, "sha256": actual, "bytes": destination.stat().st_size,
            "expected_sha256": expected}


def main():
    jobs = [(f"models/ocr/{name}", f"https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/{OCR_REVISION}/onnx/PP-OCRv5/{kind}/{name}", digest) for kind, name, digest in OCR]
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md"):
        jobs.append((f"models/asr/small/{name}", f"https://huggingface.co/Systran/faster-whisper-small/resolve/{REVISION}/{name}",
                     ASR_HASHES[name]))
    jobs.extend([
        ("models/ocr/LICENSE", "https://raw.githubusercontent.com/RapidAI/RapidOCR/v3.9.2/LICENSE", None),
        ("models/ocr/default_models.yaml", "https://raw.githubusercontent.com/RapidAI/RapidOCR/v3.9.2/python/rapidocr/default_models.yaml", None),
        ("models/asr/LICENSE.faster-whisper", "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/LICENSE", None),
        ("models/asr/LICENSE.whisper", "https://raw.githubusercontent.com/openai/whisper/v20250625/LICENSE", None),
        ("models/asr/LICENSE.silero-vad", "https://raw.githubusercontent.com/snakers4/silero-vad/v6.0/LICENSE", None),
    ])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(lambda args: download(*args), jobs))
    vad_candidates = list((ROOT / "runtime/media-model-venv").glob("**/faster_whisper/assets/silero_vad_v6.onnx"))
    if len(vad_candidates) != 1:
        raise ValueError("Expected exactly one bundled faster-whisper VAD model")
    vad = vad_candidates[0]
    with vad.open("rb") as source:
        vad_hash = hashlib.file_digest(source, "sha256").hexdigest()
    if vad_hash != "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2":
        raise ValueError("Bundled VAD model checksum mismatch")
    manifest = {"schema_version": 1, "ocr": {"model": "RapidAI PP-OCRv5 mobile", "revision": OCR_REVISION, "license": "Apache-2.0"},
                "asr": {"model": "Systran/faster-whisper-small", "revision": REVISION, "license": "MIT",
                        "vad": {"path": vad.relative_to(ROOT).as_posix(), "sha256": vad_hash, "license": "MIT",
                                "revision": "faster-whisper v1.2.1 bundled silero-v6", "bytes": vad.stat().st_size}}, "files": files}
    path = ROOT / "runtime/media-model-manifest.json"
    if path.is_file():
        previous = path.read_bytes()
        archive = ROOT / "runtime/media-model-manifests"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / (hashlib.sha256(previous).hexdigest() + ".json")).write_bytes(previous)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(path), "files": len(files), "bytes": sum(f["bytes"] for f in files)}))


if __name__ == "__main__":
    main()
