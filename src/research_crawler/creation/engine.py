"""Bounded SD1.5 generation with durable versions and owned native processes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time

from PIL import Image, ImageChops, ImageStat
import psutil

from ..core.records import Record, canonical_json
from ..core.sessions import controlled
from .templates import TEMPLATES

_VERIFIED = {}


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def parameters(params):
    profile = params.get("model_profile", "sd15")
    edit_mode = params.get("edit_mode", "img2img")
    if profile not in {"sd15", "instruct_pix2pix"} or edit_mode not in {"img2img", "instruction"}:
        raise ValueError("Invalid creation model profile or edit mode")
    instruction = profile == "instruct_pix2pix"
    if instruction != (edit_mode == "instruction"):
        raise ValueError("Instruction mode requires its explicit instruction model profile")
    if instruction and "strength" in params:
        raise ValueError("Instruction reference conditioning does not use img2img strength")
    if not instruction and "image_cfg_scale" in params:
        raise ValueError("Image guidance requires the instruction model profile")
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("Supply a prompt of 1–4000 characters")
    result = {"prompt": prompt.strip()}
    for key, default, lower, upper in (("width", 512, 256, 512), ("height", 512, 256, 512),
            ("steps", 20, 4, 30), ("iterations", 1, 1, 3), ("seed", 42, 0, 2147483644)):
        value = params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError("Invalid " + key)
        result[key] = value
    if result["width"] % 64 or result["height"] % 64:
        raise ValueError("Image dimensions must be multiples of 64")
    for key, default, lower, upper in (("strength", .65, .1, .9), ("cfg_scale", 7., 1., 12.)):
        value = params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Invalid " + key)
        result[key] = value
    negative = params.get("negative_prompt", "blurry, low quality, distorted, text, watermark")
    if not isinstance(negative, str) or len(negative) > 2000:
        raise ValueError("Invalid negative_prompt")
    result["negative_prompt"] = negative
    template = params.get("template")
    if template:
        if template not in TEMPLATES:
            raise ValueError("Unknown original template")
        result["template"] = {"id": template, **TEMPLATES[template]}
        result["prompt"] += ", " + TEMPLATES[template]["suffix"]
    result.update(sampling_method="euler", scheduler="discrete", cpu_threads=4,
                  clip_on_cpu=True, vae_on_cpu=True, model_weight_type="f16")
    if instruction:
        image_cfg = params.get("image_cfg_scale", 1.5)
        if isinstance(image_cfg, bool) or not isinstance(image_cfg, (int, float)) or not math.isfinite(image_cfg) or not 0 <= image_cfg <= 3:
            raise ValueError("image_cfg_scale must be a finite number from 0 to 3")
        result.pop("strength")
        result.update(model_profile=profile, edit_mode=edit_mode, image_cfg_scale=image_cfg,
                      sampling_method="euler_a", batched_cfg=False)
    return result


def verified_manifest(root, profile="sd15"):
    if profile not in {"sd15", "instruct_pix2pix"}:
        raise ValueError("Unknown creation model profile")
    filename = "creation-instruction-manifest.json" if profile == "instruct_pix2pix" else "creation-manifest.json"
    raw = (root / "runtime" / filename).read_bytes()
    manifest = json.loads(raw)
    items = [manifest["model"], *manifest["binaries"]]
    if profile == "instruct_pix2pix":
        if manifest.get("profile") != profile:
            raise ValueError("Instruction manifest profile mismatch")
        items.append(manifest["encoders"])
    fingerprint = tuple((item["path"], (root / item["path"]).stat().st_size,
                        (root / item["path"]).stat().st_mtime_ns) for item in items)
    key = (str(root.resolve()), hashlib.sha256(raw).hexdigest(), fingerprint)
    if key in _VERIFIED:
        return copy.deepcopy(_VERIFIED[key])
    for item in items:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or sha256(path) != item["sha256"]:
            raise ValueError("Creation runtime checksum mismatch")
    manifest["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    _VERIFIED.clear()
    _VERIFIED[key] = copy.deepcopy(manifest)
    return manifest


def snapshot_reference(context, folder):
    value = context.params.get("reference_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("reference_path must be an absolute local image path")
    source = Path(value).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > 20_000_000:
        raise ValueError("Reference must be a local image below 20MB")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = context.params.get("reference_sha256")
    if expected and expected != digest:
        raise ValueError("Reference hash mismatch")
    destination = folder / ("reference-original" + source.suffix.lower())
    destination.write_bytes(data)
    with Image.open(destination) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Reference exceeds 16MP")
        image.convert("RGB").save(folder / "reference.png")
    return {"source_path": str(source), "sha256": digest, "bytes": len(data),
            "snapshot_path": str(destination), "inference_path": str(folder / "reference.png"),
            "inference_sha256": sha256(folder / "reference.png")}


def command(root, manifest, params, output, reference=None):
    instruction = params.get("model_profile") == "instruct_pix2pix"
    if instruction and reference is None:
        raise ValueError("Instruction editing requires a frozen reference")
    model_file = manifest["encoders"]["path"] if instruction else manifest["model"]["path"]
    args = [str(root / manifest["backend"]["executable"]), "-m", str(root / model_file),
            "-p", params["prompt"], "-n", params["negative_prompt"], "-o", str(output),
            "-W", str(params["width"]), "-H", str(params["height"]), "--steps", str(params["steps"]),
            "--seed", str(params["seed"]), "--cfg-scale", str(params["cfg_scale"]),
            "--sampling-method", params["sampling_method"], "--scheduler", params["scheduler"],
            "--clip-on-cpu", "--vae-on-cpu", "--type", "f16", "-t", "4"]
    if instruction:
        args.extend(["--diffusion-model", str(root / manifest["model"]["path"]),
                     "-r", reference["inference_path"], "--img-cfg-scale", str(params["image_cfg_scale"]),
                     "--batched-cfg", "off"])
    elif reference:
        args.extend(["-i", reference["inference_path"], "--strength", str(params["strength"])])
    return args


async def run_native(context, args, log_path, coordinator=None):
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE"}}
    env.update(TEMP=str(context.paths.root / "temp"), TMP=str(context.paths.root / "temp"),
               PATH=str(Path(args[0]).parent), CUDA_CACHE_PATH=str(context.paths.root / "cache/creation-cuda"))
    started = time.perf_counter()
    peak_rss = 0
    peak_vram_mb = 0
    with log_path.open("wb") as log:
        launch = asyncio.create_task(asyncio.create_subprocess_exec(*args, stdout=log, stderr=log,
            env=env, cwd=str(context.paths.root), **({"creationflags": 0x08000000} if os.name == "nt" else {})))
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
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.Error:
                    children = []
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                _, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=3)
                for child in alive:
                    child.kill()
                if alive:
                    await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)

        try:
            if coordinator:
                coordinator.track_process(process)
            async def monitor():
                nonlocal peak_rss, peak_vram_mb
                from ..core.gpu import hardware_snapshot
                waiting = asyncio.create_task(process.wait())
                sampled = 0.
                try:
                    while not waiting.done():
                        try:
                            peak_rss = max(peak_rss, psutil.Process(process.pid).memory_info().rss)
                        except psutil.Error:
                            pass
                        if time.monotonic() - sampled > 2:
                            hardware = await hardware_snapshot()
                            for line in hardware.get("nvidia_smi", "").splitlines():
                                fields = line.split(",")
                                if len(fields) >= 4:
                                    peak_vram_mb = max(peak_vram_mb, float(fields[-2].strip()))
                            sampled = time.monotonic()
                        await asyncio.wait({waiting}, timeout=.25)
                finally:
                    if not waiting.done():
                        waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
            await controlled(context, monitor())
            if process.returncode:
                raise RuntimeError("Local creation worker failed; inspect owned run log")
        finally:
            await close()
            if coordinator:
                coordinator.untrack_process(process)
    return {"worker_seconds": round(time.perf_counter() - started, 3), "peak_owned_rss_bytes": peak_rss,
            "peak_host_vram_mb_sampled": peak_vram_mb, "vram_scope": "whole_GPU_including_desktop",
            "pid": process.pid, "returncode": process.returncode, "owned_process_reaped": True,
            "log_path": str(log_path)}


def evaluate(output, reference=None):
    with Image.open(output) as opened:
        image = opened.convert("RGB")
        stats = ImageStat.Stat(image)
        result = {"method": "pixel_statistics_v1", "width": image.width, "height": image.height,
                  "channel_mean": stats.mean, "channel_stddev": stats.stddev,
                  "nonblank": max(stats.stddev) > 2,
                  "semantic_prompt_adherence": "requires_human_review",
                  "aesthetic_quality": "requires_human_review"}
        if reference:
            with Image.open(reference["inference_path"]) as original:
                comparison = original.convert("RGB").resize(image.size)
            result["reference_mean_absolute_pixel_difference"] = sum(ImageStat.Stat(ImageChops.difference(image, comparison)).mean) / 3
            result["reference_comparison_scope"] = "pixel_difference_only_not_identity_or_semantic_preservation"
        return result


async def create(context, operation):
    from ..core.gpu import GPUCoordinator
    params = parameters(context.params)
    if params.get("model_profile") == "instruct_pix2pix":
        if operation != "edit":
            raise ValueError("The instruction model is an edit profile, not a text-to-image replacement")
        manifest = await asyncio.to_thread(verified_manifest, context.paths.root, "instruct_pix2pix")
    else:
        manifest = await asyncio.to_thread(verified_manifest, context.paths.root)
    stream = "creation." + operation
    folder = context.paths.run(context.job_id) / "creation"
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = await context.checkpoint(stream)
    if checkpoint:
        frozen = checkpoint["metadata"]
        if frozen["manifest"] != manifest or frozen["parameters"] != params:
            raise ValueError("Pinned creation settings changed")
        reference = frozen.get("reference")
    else:
        reference = await asyncio.to_thread(snapshot_reference, context, folder) if operation == "edit" else None
        frozen = {"manifest": manifest, "parameters": params, "reference": reference}
        (folder / "manifest.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint = await context.commit_page(stream, [], next_cursor=0, exhausted=False, metadata=frozen)
    if reference and (sha256(reference["snapshot_path"]) != reference["sha256"] or sha256(reference["inference_path"]) != reference["inference_sha256"]):
        raise ValueError("Pinned reference image changed")
    coordinator = GPUCoordinator(context.paths.root)
    completed = int(checkpoint["cursor"] or 0)
    if completed:
        for observation in context.store.observations(context.job_id):
            for artifact in observation["payload"].get("files", []):
                if sha256(artifact["path"]) != artifact["sha256"]:
                    raise ValueError("Committed creation artifact changed")
    if completed == params["iterations"]:
        return checkpoint["metadata"]["summary"]
    async with coordinator.lease("image", check_control=context.check_control):
        for index in range(completed, params["iterations"]):
            await context.check_control()
            # Waiting for another GPU job must not silently change a frozen
            # reference, and each bounded version rechecks the same input.
            if reference and (sha256(reference["snapshot_path"]) != reference["sha256"] or
                    sha256(reference["inference_path"]) != reference["inference_sha256"]):
                raise ValueError("Pinned reference image changed while queued")
            version_params = {**params, "seed": params["seed"] + index}
            identity = hashlib.sha256(canonical_json([version_params, manifest["manifest_sha256"], reference and reference["sha256"], operation]).encode()).hexdigest()
            output = folder / (identity + ".png")
            output.unlink(missing_ok=True)
            metrics = await run_native(context, command(context.paths.root, manifest, version_params, output, reference), folder / (identity + ".log"), coordinator)
            evaluation = evaluate(output, reference)
            payload = {"schema_version": 1, "version": index + 1, "version_hash": identity,
                       "operation": operation, "parameters": version_params, "model": manifest["model"],
                       "backend": manifest["backend"], "manifest_sha256": manifest["manifest_sha256"],
                       "reference": reference, "evaluation": evaluation, "metrics": metrics,
                       "files": [{"path": str(output), "sha256": sha256(output), "bytes": output.stat().st_size,
                                  "content_type": "image/png", "role": "generated_image"}]}
            (folder / (identity + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = {"versions": index + 1, "coverage_complete": index + 1 == params["iterations"],
                       "model": manifest["model"]["name"], "evaluation": "human_review_required", "output_directory": str(folder)}
            record = Record("local_creation", operation, identity, output.as_uri(), payload,
                            coverage={"complete": True, "model_generated": True, "semantic_quality_verified": False})
            checkpoint = await context.commit_page(stream, [record], next_cursor=index + 1,
                exhausted=index + 1 == params["iterations"], expected_revision=checkpoint["revision"],
                metadata={**frozen, "summary": summary})
    return checkpoint["metadata"]["summary"]
