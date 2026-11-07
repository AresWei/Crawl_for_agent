"""Local OCR/ASR jobs and a one-request CPU worker; no model imports at startup."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

OCR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}
ASR_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".webm", ".mov"}


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def limits(params):
    result = {}
    for key, default, lower, upper in [("max_files", 8, 1, 32), ("max_bytes", 268435456, 1, 1073741824),
            ("max_pages", 20, 1, 100), ("max_pixels", 25000000, 1024, 50000000),
            ("max_duration_seconds", 600, 1, 3600), ("cpu_threads", 4, 1, 6)]:
        value = params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not lower <= value <= upper:
            raise ValueError(f"Invalid {key}")
        result[key] = int(value)
    language = params.get("language")
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)):
        raise ValueError("language must be an ISO language code or null for detection")
    result["language"] = language
    return result


def worker_environment(root):
    # Deliberately do not inherit tokens, credentials, proxy URLs, PYTHONPATH or user config.
    env = {key: value for key, value in os.environ.items() if key.upper() in
           {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE"}}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1",
                "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4"})
    for key, relative in {"TEMP": "temp", "TMP": "temp", "HF_HOME": "cache/huggingface", "XDG_CACHE_HOME": "cache",
                          "HF_HUB_CACHE": "cache/huggingface/hub", "TORCH_HOME": "cache/torch"}.items():
        env[key] = str(root / relative)
    return env


async def run_worker(context, request):
    from ..core.sessions import controlled
    root = context.paths.root
    interpreter = root / "runtime/media-model-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        raise RuntimeError("Media model worker is not installed")
    launch = asyncio.create_task(asyncio.create_subprocess_exec(str(interpreter), str(Path(__file__).resolve()),
        "--worker", str(root), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=worker_environment(root), cwd=str(root),
        **({"creationflags": 0x08000000} if os.name == "nt" else {})))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await asyncio.shield(launch)
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise

    async def close():
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    context.resources.push_async_callback(close)
    started = time.perf_counter()
    try:
        output, _ = await controlled(context, process.communicate(json.dumps(request, ensure_ascii=False).encode("utf-8")))
        if len(output) > 16000000:
            raise RuntimeError("Media worker output limit exceeded")
        result = json.loads(output)
        if process.returncode or not result.get("ok"):
            if result.get("error") in {"audio_duration_budget", "page_budget", "image_pixel_budget"}:
                from ..core.jobs import BudgetExceeded
                raise BudgetExceeded(result["error"])
            raise RuntimeError("Media worker failed: " + str(result.get("error", "worker_failure")))
        result["metrics"]["worker_wall_seconds"] = round(time.perf_counter() - started, 3)
        return result
    finally:
        await close()


def select_inputs(context, operation, bounds):
    explicit = context.params.get("files")
    source_job = context.params.get("source_job_id")
    selected = context.params.get("record_keys")
    if explicit is not None and (source_job or selected):
        raise ValueError("Choose explicit files or source_job_id with optional record_keys")
    candidates = []
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit:
            raise ValueError("files must be a nonempty list")
        for item in explicit:
            item = {"path": item} if isinstance(item, str) else item
            if not isinstance(item, dict):
                raise ValueError("Invalid file selection")
            candidates.append({"path": item.get("path"), "expected_sha256": item.get("sha256"), "selection": "explicit_local_file"})
    elif source_job:
        from ..core.paths import safe_name
        safe_name(source_job)
        context.store.job(source_job)
        if selected is not None and (not isinstance(selected, list) or not selected or len(selected) > 1000
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in selected)):
            raise ValueError("Invalid record_keys")
        if context.store.counts(source_job)["versions"] > 10000:
            raise ValueError("Source job selection exceeds 10000 observations")
        found = set()
        for offset in range(0, 10000, 1000):
            rows = context.store.observations(source_job, limit=1000, offset=offset)
            for row in rows:
                if selected and row["record_key"] not in selected:
                    continue
                found.add(row["record_key"])
                for item in row["payload"].get("files", []):
                    if not item.get("path") or not item.get("sha256"):
                        continue
                    path = Path(item["path"]).resolve()
                    if not path.is_relative_to(context.paths.run(source_job).resolve()):
                        raise ValueError("Stored media asset is outside its source run")
                    if path.suffix.lower() not in (OCR_EXTENSIONS if operation == "ocr" else ASR_EXTENSIONS):
                        continue
                    candidates.append({"path": str(path), "expected_sha256": item["sha256"], "selection": "stored_media_asset",
                        "source_job_id": source_job, "record_key": row["record_key"], "record_content_hash": row["content_hash"],
                        "source_url": row["url"], "source_provenance": row["payload"].get("provenance", {})})
            if len(rows) < 1000:
                break
        if selected and set(selected) != found:
            raise ValueError("Selected record was not found in source job")
    else:
        raise ValueError("Supply files or source_job_id")
    if not candidates or len(candidates) > bounds["max_files"]:
        raise ValueError("Media input count outside budget")
    total = 0
    for item in candidates:
        value = item["path"]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("Input must be an absolute local file path")
        path = Path(value).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in (OCR_EXTENSIONS if operation == "ocr" else ASR_EXTENSIONS):
            raise ValueError("Unsupported media input")
        if item.get("expected_sha256") and not re.fullmatch(r"[a-fA-F0-9]{64}", item["expected_sha256"]):
            raise ValueError("Invalid source digest")
        total += path.stat().st_size
        if total > bounds["max_bytes"]:
            raise ValueError("Media source byte budget exceeded")
        item["path"] = str(path)
        item["bytes"] = path.stat().st_size
    return candidates


async def snapshot_input(context, item, index, maximum_bytes):
    folder = context.paths.run(context.job_id) / "media-analysis/inputs"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (str(index) + Path(item["path"]).suffix.lower())
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with Path(item["path"]).open("rb") as source, temporary.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ValueError("Source changed beyond byte budget")
                digest.update(chunk)
                output.write(chunk)
                await context.check_control()
        actual = digest.hexdigest()
        if item.get("expected_sha256") and actual != item["expected_sha256"].lower():
            raise ValueError("Source artifact checksum mismatch")
        temporary.replace(destination)
        return {**{k: v for k, v in item.items() if k != "expected_sha256"},
                "sha256": actual, "bytes": size, "snapshot_path": str(destination)}
    finally:
        temporary.unlink(missing_ok=True)


async def analyze(context, operation):
    from ..core.records import Record, canonical_json
    bounds = limits(context.params)
    manifest_path = context.paths.root / "runtime/media-model-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    model = {**manifest["ocr" if operation == "ocr" else "asr"], "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "device": "cpu"}
    stream = "media." + ("ocr" if operation == "ocr" else "transcribe")
    checkpoint = await context.checkpoint(stream)
    metadata = checkpoint.get("metadata", {}) if checkpoint else {}
    inputs = metadata.get("inputs")
    folder = context.paths.run(context.job_id) / "media-analysis"
    folder.mkdir(parents=True, exist_ok=True)
    manifest_snapshot = folder / "model-manifest.json"
    if not manifest_snapshot.exists():
        manifest_snapshot.write_bytes(manifest_bytes)
    if sha256(manifest_snapshot) != model["manifest_sha256"]:
        raise ValueError("Frozen media model manifest changed")
    if not inputs:
        candidates = select_inputs(context, operation, bounds)
        inputs, remaining = [], bounds["max_bytes"]
        for index, item in enumerate(candidates):
            snapshot = await snapshot_input(context, item, index, remaining)
            inputs.append(snapshot)
            remaining -= snapshot["bytes"]
        checkpoint = await context.commit_page(stream, [], next_cursor=0, exhausted=False,
            expected_revision=checkpoint["revision"] if checkpoint else 0, metadata={"inputs": inputs, "model": model})
    elif metadata.get("model") != model:
        raise ValueError("Pinned media model changed; create a fresh analysis job")
    completed = int(checkpoint["cursor"] or 0)
    for index in range(completed, len(inputs)):
        await context.check_control()
        source = inputs[index]
        response = await run_worker(context, {"operation": operation, "path": source["snapshot_path"],
            "sha256": source["sha256"], "limits": bounds})
        result = response["result"]
        payload = {"schema_version": 1, "source_artifact": source, "model": model,
                   "model_manifest": {"path": str(manifest_snapshot), "sha256": model["manifest_sha256"]},
                   "analysis": result, "text": result["text"], "metrics": response["metrics"]}
        # Equal bytes can have distinct evidence origins. Include the frozen
        # selection position and provenance so a later input cannot overwrite
        # an already committed artifact; exclude only the job-specific copy path.
        origin = {key: value for key, value in source.items() if key != "snapshot_path"}
        identity = hashlib.sha256(canonical_json([origin, model, operation, bounds, index]).encode()).hexdigest()
        output_path = folder / (identity + ".json")
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        payload["artifact"] = {"path": str(output_path), "sha256": sha256(output_path)}
        payload["files"] = [{**payload["artifact"], "bytes": output_path.stat().st_size, "content_type": "application/json"}]
        text_path = folder / (identity + ".txt")
        text_path.write_text(result["text"], encoding="utf-8")
        payload["files"].append({"path": str(text_path), "sha256": sha256(text_path),
                                 "bytes": text_path.stat().st_size, "content_type": "text/plain; charset=utf-8"})
        record = Record("local_media", "ocr" if operation == "ocr" else "transcript", identity,
            source.get("source_url") or Path(source["path"]).as_uri(), payload,
            coverage={"complete": True, "scope": "selected_local_artifact", "model_generated": True,
                      "pages": len(result.get("pages", [])), "duration_seconds": result.get("duration_seconds")})
        summary = {"files": index + 1, "coverage_complete": index + 1 == len(inputs), "device": "cpu", "model": model["model"]}
        checkpoint = await context.commit_page(stream, [record], next_cursor=index + 1, exhausted=index + 1 == len(inputs),
            expected_revision=checkpoint["revision"], metadata={"inputs": inputs, "model": model, "summary": summary})
    return {"files": len(inputs), "coverage_complete": True, "device": "cpu", "model": model["model"]}


def verify_models(root, operation):
    manifest = json.loads((root / "runtime/media-model-manifest.json").read_text(encoding="utf-8-sig"))
    prefix = "models/ocr/" if operation == "ocr" else "models/asr/"
    for entry in manifest["files"]:
        if entry["path"].startswith(prefix) and sha256(root / entry["path"]) != entry["sha256"]:
            raise ValueError("model_integrity")
    if operation == "asr" and manifest["asr"].get("vad"):
        vad = manifest["asr"]["vad"]
        if sha256(root / vad["path"]) != vad["sha256"]:
            raise ValueError("vad_model_integrity")


def recognize(root, request):
    import numpy as np
    from PIL import Image
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import OCRVersion, ModelType
    bound = request["limits"]
    Image.MAX_IMAGE_PIXELS = bound["max_pixels"]
    params = {"Global.log_level": "error", "Global.model_root_dir": str(root / "models/ocr"),
              "EngineConfig.onnxruntime.intra_op_num_threads": bound["cpu_threads"],
              "EngineConfig.onnxruntime.inter_op_num_threads": 1, "EngineConfig.onnxruntime.use_cuda": False}
    for stage, filename in [("Det", "ch_PP-OCRv5_det_mobile.onnx"), ("Rec", "ch_PP-OCRv5_rec_mobile.onnx"),
                            ("Cls", "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx")]:
        params.update({f"{stage}.model_path": str(root / "models/ocr" / filename),
                       f"{stage}.ocr_version": OCRVersion.PPOCRV5, f"{stage}.model_type": ModelType.MOBILE})
    engine = RapidOCR(params=params)
    pages = []

    def process(image, number, coordinate_space="image_pixels"):
        if image.width * image.height > bound["max_pixels"]:
            raise ValueError("image_pixel_budget")
        output = engine(np.asarray(image.convert("RGB"))[:, :, ::-1])
        lines = []
        if output.txts is not None:
            for box, text, confidence in zip(output.boxes, output.txts, output.scores):
                lines.append({"box": [[round(float(x), 2), round(float(y), 2)] for x, y in box],
                              "text": text, "confidence": round(float(confidence), 6)})
        pages.append({"page": number, "width": image.width, "height": image.height,
                      "coordinate_space": coordinate_space, "lines": lines})

    path = Path(request["path"])
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(str(path)) as document:
            if len(document) > bound["max_pages"]:
                raise ValueError("page_budget")
            for index in range(len(document)):
                page = document[index]
                try:
                    width, height = page.get_size()
                    if math.ceil(width * 2) * math.ceil(height * 2) > bound["max_pixels"]:
                        raise ValueError("image_pixel_budget")
                    bitmap = page.render(scale=2)
                    try:
                        process(bitmap.to_pil(), index + 1, "rendered_pixels_144dpi")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    else:
        with Image.open(path) as image:
            if getattr(image, "n_frames", 1) > bound["max_pages"]:
                raise ValueError("page_budget")
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                process(image, index + 1)
    return {"pages": pages, "text": "\n\n".join("\n".join(line["text"] for line in page["lines"]) for page in pages)}


def bounded_audio(path, maximum_seconds):
    import av
    import numpy as np
    chunks, samples = [], 0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(str(path), mode="r", options={"protocol_whitelist": "file"}) as container:
        if not container.streams.audio:
            raise ValueError("no_audio_stream")
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                data = converted.to_ndarray().reshape(-1)
                samples += data.size
                if samples > maximum_seconds * 16000:
                    raise ValueError("audio_duration_budget")
                chunks.append(data)
        for converted in resampler.resample(None):
            data = converted.to_ndarray().reshape(-1)
            samples += data.size
            if samples > maximum_seconds * 16000:
                raise ValueError("audio_duration_budget")
            chunks.append(data)
    if not samples:
        raise ValueError("empty_audio")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(root, request):
    from faster_whisper import WhisperModel
    bound = request["limits"]
    audio = bounded_audio(request["path"], bound["max_duration_seconds"])
    model = WhisperModel(str(root / "models/asr/small"), device="cpu", compute_type="int8",
                         cpu_threads=bound["cpu_threads"], num_workers=1, local_files_only=True)
    segments, info = model.transcribe(audio, language=bound["language"], beam_size=3, word_timestamps=True,
        vad_filter=True, condition_on_previous_text=False, temperature=0.0)
    result = []
    for segment in segments:
        result.append({"id": segment.id, "start": round(segment.start, 3), "end": round(segment.end, 3),
            "text": segment.text, "avg_logprob": segment.avg_logprob, "no_speech_probability": segment.no_speech_prob,
            "words": [{"word": word.word, "start": round(word.start, 3), "end": round(word.end, 3),
                       "probability": round(word.probability, 6)} for word in (segment.words or [])]})
    return {"text": "".join(segment["text"] for segment in result).strip(), "segments": result,
            "duration_seconds": round(len(audio) / 16000, 3), "language": info.language,
            "language_probability": info.language_probability, "compute_type": "int8", "vad_filter": True,
            "audio_stream_index": 0, "timestamps": "seconds_from_decoded_audio_start"}


def worker_main(root):
    import psutil
    started = time.perf_counter()
    request = json.loads(sys.stdin.buffer.read(65537))
    if request.get("operation") not in {"ocr", "asr"}:
        raise ValueError("operation")
    path = Path(request["path"]).resolve(strict=True)
    if not path.is_relative_to((root / "runs").resolve()) or sha256(path) != request["sha256"]:
        raise ValueError("input_integrity")
    request["limits"] = limits(request["limits"])
    verify_models(root, request["operation"])
    with redirect_stdout(sys.stderr):
        result = recognize(root, request) if request["operation"] == "ocr" else transcribe(root, request)
    memory = psutil.Process().memory_info()
    return {"ok": True, "result": result, "metrics": {"model_seconds": round(time.perf_counter() - started, 3),
            "rss_bytes": memory.rss, "peak_working_set_bytes": getattr(memory, "peak_wset", None),
            "device": "cpu", "gpu_requested": False, "cpu_threads": request["limits"]["cpu_threads"]}}


if __name__ == "__main__":
    try:
        response = worker_main(Path(sys.argv[2]).resolve())
    except Exception as error:
        # Paths and source content may contain private information. Never emit traceback, environment or arbitrary error text.
        safe_codes = {"audio_duration_budget", "page_budget", "image_pixel_budget", "model_integrity",
                      "vad_model_integrity", "input_integrity", "empty_audio", "no_audio_stream"}
        response = {"ok": False, "error": str(error) if str(error) in safe_codes else type(error).__name__}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    raise SystemExit(0 if response["ok"] else 1)
