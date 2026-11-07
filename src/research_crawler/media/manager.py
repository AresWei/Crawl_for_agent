"""Independent downloader; platform extraction stays in the migrated adapters.

No external downloader backend, credential serialization, or remote media upload.
Files are immutable only after size/hash checks; partial files are job-local.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..core.jobs import BudgetExceeded, JobControl
from ..core.records import Record, canonical_json, utcnow
from ..core.sessions import controlled, open_http

CAPABILITIES = {
    "implemented": ["selected_record_media", "authorized_url_download", "images", "covers", "videos",
        "live_photo_pairs", "dash_audio_video", "existing_ffmpeg_mux", "conditional_range_resume",
        "sha256_integrity", "shared_accounts_proxy_budgets", "cancel_pause_retry", "durl_segment_concatenation"],
    "job": "media.download", "concurrent_jobs_per_runner": 1,
    "limits": {"max_files": 200, "max_bytes": 2_000_000_000, "max_retries": 4},
    "pending": [], "authenticated_platform_live_verified": False,
}

REFERERS = {"bili": "https://www.bilibili.com/", "xhs": "https://www.xiaohongshu.com/",
    "dy": "https://www.douyin.com/", "ks": "https://www.kuaishou.com/", "wb": "https://weibo.com/",
    "douyin": "https://www.douyin.com/", "kuaishou": "https://www.kuaishou.com/", "weibo": "https://weibo.com/"}
RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")


class MediaError(Exception):
    """Safe reason codes only; upstream exception messages can include secrets."""


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical_json(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def checked_url(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError("Media URL must be an HTTP(S) URL")
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Media URL must be HTTP(S), without embedded credentials")
    return value


def normalize(candidate):
    if isinstance(candidate, str):
        candidate = {"url": candidate, "kind": "file"}
    if not isinstance(candidate, dict):
        raise ValueError("Media candidates must be URLs or objects")
    if candidate.get("drm") or candidate.get("encrypted"):
        raise ValueError("Encrypted media is not supported")
    kind = candidate.get("kind", "file")
    if kind not in {"image", "video", "audio", "live_photo", "file", "cover"}:
        raise ValueError("Unsupported media kind")
    result = {"url": checked_url(candidate.get("url")), "kind": kind}
    for field in ("role", "index", "quality", "available_qualities", "format", "requires_mux",
                  "requires_concat", "segment_index", "segment_count", "segment_group", "complete", "sha256", "size"):
        if field in candidate:
            result[field] = candidate[field]
    if result.get("sha256") and not re.fullmatch(r"[a-fA-F0-9]{64}", str(result["sha256"])):
        raise ValueError("Expected a SHA-256 digest")
    for field in ("audio_url", "image_url"):
        if candidate.get(field):
            result[field] = checked_url(candidate[field])
    for field, alias in (("backup_urls", "backups"), ("audio_backup_urls", "audio_backups")):
        values = candidate.get(field, candidate.get(alias, [])) or []
        if not isinstance(values, list) or len(values) > 10:
            raise ValueError("At most ten backup media URLs may be supplied")
        result[field] = list(dict.fromkeys(checked_url(value) for value in values))
    return result


def selected_records(context):
    """Resolve a bounded, frozen selection; the manifest retains exact versions."""
    params, rows = context.params, []
    keys = params.get("record_keys")
    if keys is not None and (not isinstance(keys, list) or not 1 <= len(keys) <= 500
            or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in keys)):
        raise ValueError("record_keys must contain 1..500 stored record keys")
    source_job_id = params.get("source_job_id")
    if source_job_id:
        context.store.job(source_job_id)
    if keys:
        with context.store.connection() as db:
            for key in keys:
                source_filter = " AND o.job_id=?" if source_job_id else ""
                row = db.execute("""SELECT o.*,r.source,r.kind,r.native_id,v.payload_json,
                    v.source_date,v.source_date_field FROM observations o JOIN records r ON r.key=o.record_key
                    JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
                    WHERE o.record_key=?""" + source_filter + " ORDER BY o.observed_at DESC LIMIT 1",
                    (key, source_job_id) if source_job_id else (key,)).fetchone()
                if row:
                    item = dict(row)
                    item["payload"] = json.loads(item.pop("payload_json"))
                    rows.append(item)
    elif source_job_id:
        for offset in range(0, 10000, 1000):
            page = context.store.observations(source_job_id, limit=1000, offset=offset)
            rows.extend(page)
            if len(page) < 1000:
                break
        if context.store.counts(source_job_id)["versions"] > 10000:
            raise ValueError("Select record_keys from a source job with over 10000 versions")
    latest = {row["record_key"]: row for row in rows if keys is None or row["record_key"] in keys}
    if keys and set(keys) != set(latest):
        raise ValueError("Some selected record keys were not found in the requested source")
    return list(latest.values())


def resolve_assets(context):
    params, assets = context.params, []
    explicit = params.get("urls")
    if explicit is not None and (params.get("source_job_id") or params.get("record_keys")):
        raise ValueError("Choose stored records or explicit URLs")
    if explicit is not None:
        if not isinstance(explicit, list) or not 1 <= len(explicit) <= 200:
            raise ValueError("urls must contain 1..200 authorized media URLs")
        inputs = [(candidate, {"selection": "explicit_url", "platform": params.get("platform", "web")}) for candidate in explicit]
    else:
        if not params.get("source_job_id") and not params.get("record_keys"):
            raise ValueError("Supply source_job_id, record_keys or explicit authorized urls")
        inputs = []
        for row in selected_records(context):
            provenance = {"source_job_id": row["job_id"], "record_key": row["record_key"],
                "content_hash": row["content_hash"], "source_url": row["url"], "platform": row["source"],
                "source_date": row["source_date"], "source_date_field": row["source_date_field"],
                "source_observed_at": row["observed_at"]}
            media = row["payload"].get("media") or []
            if not isinstance(media, list):
                continue
            inputs.extend((candidate, provenance) for candidate in media)
            if len(inputs) > 5000:
                raise ValueError("Select fewer than 5000 media candidates")
    for candidate, provenance in inputs:
        candidate = normalize(candidate)
        identity = hashlib.sha256(canonical_json([provenance, candidate]).encode()).hexdigest()[:24]
        assets.append({"id": identity, "candidate": candidate, "provenance": provenance})
    return group_segments(assets)


def group_segments(assets):
    """One source version owns a sequence; never combine unrelated records/versions."""
    result, groups = [], {}
    for asset in assets:
        candidate = asset["candidate"]
        if not (candidate.get("requires_concat") or candidate.get("segment_count", 1) != 1):
            result.append(asset)
            continue
        key = canonical_json([asset["provenance"], candidate.get("format"),
            candidate.get("quality"), candidate.get("segment_group")])
        if key not in groups:
            group = {"candidate": dict(candidate), "provenance": asset["provenance"], "segments": []}
            groups[key] = group
            result.append(group)
        groups[key]["segments"].append(asset)
    for group in groups.values():
        segments = group["segments"]
        counts = [item["candidate"].get("segment_count") for item in segments]
        indices = [item["candidate"].get("segment_index") for item in segments]
        count = counts[0]
        valid = (type(count) is int and count > 1 and count == len(segments) and all(value == count for value in counts)
            and all(type(value) is int for value in indices)
            and sorted(indices) == list(range(1, count + 1)))
        if valid:
            segments.sort(key=lambda item: item["candidate"]["segment_index"])
        group["segment_error"] = None if valid else "segments_incomplete"
        group["candidate"] = dict(segments[0]["candidate"])
        group["id"] = hashlib.sha256(canonical_json([group["provenance"],
            [item["candidate"] for item in segments]]).encode()).hexdigest()[:24]
    return result


class DiskBudget:
    """Bound retained originals and derived files, including resumable partials."""
    def __init__(self, folder, maximum_bytes, maximum_files):
        self.folder, self.maximum_bytes, self.maximum_files = folder, maximum_bytes, maximum_files

    def files(self):
        return [path for path in self.folder.iterdir() if path.is_file() and path.suffix not in {".json", ".tmp"}]

    def remaining(self):
        return self.maximum_bytes - sum(path.stat().st_size for path in self.files())

    def allocate(self, path):
        if not path.exists() and len(self.files()) >= self.maximum_files:
            raise BudgetExceeded("media_file_budget")

    def received(self, length):
        if length > self.remaining():
            raise BudgetExceeded("media_byte_budget")


def file_identity(platform, url):
    return hashlib.sha256(canonical_json([platform, url]).encode()).hexdigest()[:32]


def mime_extension(mime, kind):
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/avif": ".avif", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
        "image/svg+xml": ".svg", "video/mp4": ".mp4", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
        "video/webm": ".webm", "audio/webm": ".webm", "video/x-flv": ".flv"}.get(mime,
        ".mp4" if kind in {"video", "live_photo"} else ".m4a" if kind == "audio" else ".bin")


def validator(headers):
    etag = headers.get("etag", "")
    if etag and not etag.startswith("W/"):
        return "etag", etag
    modified = headers.get("last-modified", "")
    return ("last-modified", modified) if modified else (None, None)


async def download_file(context, folder, budget, platform, url, backups, kind, *, transport=None,
                        expected_hash=None, expected_size=None):
    identity = file_identity(platform, url)
    part, state_path = folder / (identity + ".part"), folder / (identity + ".json")
    state = read_json(state_path)
    if state.get("status") == "complete":
        final = folder / state["filename"]
        if final.parent == folder and final.is_file() and final.stat().st_size == state.get("size"):
            actual = await asyncio.to_thread(digest, final)
            if actual == state.get("sha256") and (not expected_hash or actual == expected_hash.lower()) and (expected_size is None or final.stat().st_size == int(expected_size)):
                return {key: state[key] for key in ("filename", "size", "sha256", "content_type", "download_url", "completed_at")}
        if final.parent == folder:
            final.unlink(missing_ok=True)
        state = {}
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    if platform in REFERERS:
        headers["Referer"] = REFERERS[platform]
    account = (context.params.get("accounts") or {}).get(platform, context.params.get("account"))
    client = await open_http(context, platform=platform, account=account, headers=headers, transport=transport)
    retries = int(context.params.get("retries", 2))
    if not 0 <= retries <= 4:
        raise ValueError("retries must be 0..4")
    urls = [url] + [value for value in backups if value != url]
    last_reason = "download_failed"
    for attempt in range((retries + 1) * len(urls)):
        await context.check_control()
        chosen = urls[min(attempt // (retries + 1), len(urls) - 1)]
        request_headers = {}
        offset = part.stat().st_size if part.exists() else 0
        can_resume = (offset > 0 and state.get("download_url") == chosen and state.get("range_supported")
            and state.get("validator") and state.get("size") == offset
            and state.get("sha256") == await asyncio.to_thread(digest, part))
        if can_resume:
            request_headers = {"Range": f"bytes={offset}-", "If-Range": state["validator"]}
        elif offset:
            part.unlink()
            offset = 0
        budget.allocate(part)
        try:
            async with client.stream("GET", chosen, headers=request_headers, follow_redirects=True) as response:
                if response.status_code not in {200, 206}:
                    raise MediaError("http_" + str(response.status_code))
                content_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise MediaError("unexpected_content_encoding")
                if content_type in {"text/html", "application/json", "application/xml"}:
                    raise MediaError("non_media_response")
                if content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
                    raise MediaError("streaming_manifest_not_supported")
                length_header = response.headers.get("content-length")
                content_length = int(length_header) if length_header is not None else None
                if content_length is not None and content_length < 0:
                    raise MediaError("invalid_content_length")
                name, current_validator = validator(response.headers)
                expected_total = content_length
                if response.status_code == 206:
                    matched = RANGE.fullmatch(response.headers.get("content-range", ""))
                    if not matched:
                        raise MediaError("invalid_content_range")
                    start, end, total = map(int, matched.groups())
                    if start != offset or end < start or end >= total or (content_length is not None and content_length != end - start + 1):
                        raise MediaError("invalid_content_range")
                    if offset and (not can_resume or current_validator != state["validator"] or name != state.get("validator_name") or state.get("total") not in {None, total}):
                        part.unlink(missing_ok=True)
                        state = {}
                        raise MediaError("representation_changed")
                    expected_total = total
                elif offset:
                    # A successful full response means If-Range failed or Range was ignored.
                    part.unlink(missing_ok=True)
                    offset = 0
                state = {"status": "partial", "download_url": chosen, "content_type": content_type,
                    "validator_name": name, "validator": current_validator, "total": expected_total,
                    "range_supported": response.status_code == 206 or response.headers.get("accept-ranges", "").lower() == "bytes"}
                atomic_json(state_path, state)
                with part.open("ab" if offset else "wb") as handle:
                    try:
                        async for chunk in response.aiter_raw():
                            budget.received(len(chunk))
                            handle.write(chunk)
                            handle.flush()
                    finally:
                        handle.flush()
                        os.fsync(handle.fileno())
                size = part.stat().st_size
                if not size or (expected_total is not None and size != expected_total):
                    raise MediaError("incomplete_response")
                actual_hash = await asyncio.to_thread(digest, part)
                if expected_size is not None and size != int(expected_size):
                    part.unlink()
                    raise MediaError("source_size_mismatch")
                if expected_hash and actual_hash != expected_hash.lower():
                    part.unlink()
                    raise MediaError("source_hash_mismatch")
                final = folder / (identity + mime_extension(content_type, kind))
                await context.check_control()
                os.replace(part, final)
                state.update(status="complete", filename=final.name, size=size, sha256=actual_hash, completed_at=utcnow())
                atomic_json(state_path, state)
                return {key: state[key] for key in ("filename", "size", "sha256", "content_type", "download_url", "completed_at")}
        except (httpx.HTTPError, MediaError, ValueError) as error:
            last_reason = str(error) if isinstance(error, MediaError) else type(error).__name__
        finally:
            if part.exists():
                state.update(status="partial", size=part.stat().st_size, sha256=await asyncio.to_thread(digest, part))
                atomic_json(state_path, state)
    raise MediaError(last_reason)


async def process(context, arguments):
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*arguments, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=context.paths.root, env=context.paths.child_environment(),
        **({'creationflags':0x08000000} if os.name=='nt' else {})))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await asyncio.shield(launch)
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    try:
        output, _ = await controlled(context, process.communicate())
        if process.returncode:
            raise MediaError("media_process_failed")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def packet_counts(context, executable, path):
    raw = await process(context, [executable, "-v", "error", "-protocol_whitelist", "file", "-count_packets",
        "-show_entries", "stream=codec_type,nb_read_packets", "-of", "json", str(path)])
    try:
        streams = json.loads(raw)["streams"]
        return {kind: sum(int(item["nb_read_packets"]) for item in streams if item.get("codec_type") == kind)
                for kind in ("video", "audio")}
    except (KeyError, ValueError, TypeError):
        raise MediaError("mux_integrity_unverifiable") from None


async def mux(context, folder, budget, identity, video, audio):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaError("ffmpeg_or_ffprobe_unavailable")
    output, temporary = folder / (identity + ".mux.mp4"), folder / (identity + ".mux.part.mp4")
    state_path = folder / (identity + ".mux.json")
    inputs = [video["sha256"], audio["sha256"]]
    state = read_json(state_path)
    if output.is_file() and state.get("inputs") == inputs and state.get("sha256") == await asyncio.to_thread(digest, output):
        return {key: state[key] for key in ("filename", "size", "sha256", "content_type", "completed_at")}
    output.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    budget.allocate(temporary)
    available = budget.remaining()
    if available <= 0:
        raise BudgetExceeded("media_byte_budget")
    try:
        await process(context, [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
            "-protocol_whitelist", "file", "-i", str(folder / video["filename"]),
            "-protocol_whitelist", "file", "-i", str(folder / audio["filename"]),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-fs", str(available), str(temporary)])
        if not temporary.is_file() or temporary.stat().st_size > available:
            raise BudgetExceeded("media_byte_budget")
        counts = await packet_counts(context, ffprobe, temporary)
        original_video = await packet_counts(context, ffprobe, folder / video["filename"])
        original_audio = await packet_counts(context, ffprobe, folder / audio["filename"])
        if not counts["video"] or not counts["audio"] or counts != {"video": original_video["video"], "audio": original_audio["audio"]}:
            raise MediaError("mux_packet_count_mismatch")
        await context.check_control()
        os.replace(temporary, output)
        state = {"filename": output.name, "size": output.stat().st_size,
            "sha256": await asyncio.to_thread(digest, output), "content_type": "video/mp4",
            "completed_at": utcnow(), "inputs": inputs, "packet_counts": counts}
        atomic_json(state_path, state)
        return {key: state[key] for key in ("filename", "size", "sha256", "content_type", "completed_at")}
    finally:
        temporary.unlink(missing_ok=True)


async def download(context, *, transport=None):
    started = time.monotonic()
    maximum_files, maximum_bytes = int(context.params.get("max_files", 20)), int(context.params.get("max_bytes", 100_000_000))
    if not 1 <= maximum_files <= 200 or not 1 <= maximum_bytes <= 2_000_000_000:
        raise ValueError("max_files must be 1..200 and max_bytes 1..2000000000")
    folder = context.paths.run(context.job_id) / "media"
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    manifest = read_json(manifest_path)
    if not manifest:
        manifest = {"version": 2, "assets": await asyncio.to_thread(resolve_assets, context), "created_at": utcnow()}
        atomic_json(manifest_path, manifest)
    elif manifest.get("version", 1) < 2:
        manifest.update(version=2, assets=group_segments(manifest["assets"]))
        atomic_json(manifest_path, manifest)
    assets, results = manifest["assets"], []
    budget = DiskBudget(folder, maximum_bytes, maximum_files)
    checkpoint = await context.checkpoint("media")
    revision = checkpoint["revision"] if checkpoint else 0
    for index, asset in enumerate(assets):
        candidate, provenance = asset["candidate"], asset["provenance"]
        checkpoint = await context.commit_page("media", [], next_cursor={"index": index, "asset_id": asset["id"]},
            exhausted=False, expected_revision=revision, metadata={"manifest": "media/manifest.json", "candidates": len(assets)})
        revision = checkpoint["revision"]
        files, reason = [], None
        try:
            if asset.get("segments"):
                from .concat import concatenate
                for segment in asset["segments"]:
                    part = segment["candidate"]
                    downloaded = await download_file(context, folder, budget, provenance["platform"], part["url"],
                        part["backup_urls"], part["kind"], transport=transport,
                        expected_hash=part.get("sha256"), expected_size=part.get("size"))
                    files.append({**downloaded, "role": "video_segment", "source_url": part["url"],
                        "segment_index": part.get("segment_index"), "segment_count": part.get("segment_count"),
                        "source_asset_id": segment["id"]})
                reason = asset.get("segment_error")
                if not reason:
                    assembled = await concatenate(context, folder, budget, asset["id"], files)
                    files.append({**assembled, "role": "concatenated_video"})
            else:
                main = await download_file(context, folder, budget, provenance["platform"], candidate["url"],
                    candidate["backup_urls"], candidate["kind"], transport=transport,
                    expected_hash=candidate.get("sha256"), expected_size=candidate.get("size"))
                files.append({**main, "role": "video" if candidate.get("audio_url") else candidate.get("role", candidate["kind"])})
            if not asset.get("segments") and candidate.get("image_url"):
                still = await download_file(context, folder, budget, provenance["platform"], candidate["image_url"], [], "image", transport=transport)
                files.append({**still, "role": "live_photo_image"})
            if not asset.get("segments") and candidate.get("audio_url"):
                audio = await download_file(context, folder, budget, provenance["platform"], candidate["audio_url"],
                    candidate["audio_backup_urls"], "audio", transport=transport)
                files.append({**audio, "role": "audio"})
                if context.params.get("mux", True):
                    assembled = await mux(context, folder, budget, asset["id"], main, audio)
                    files.append({**assembled, "role": "muxed_video"})
                else:
                    reason = "mux_disabled"
            elif candidate.get("requires_mux"):
                reason = "audio_stream_unavailable"
            if not asset.get("segments") and candidate.get("complete") is False:
                reason = reason or "source_media_incomplete"
            if candidate["kind"] == "live_photo" and not candidate.get("image_url"):
                reason = "live_photo_image_unavailable"
        except MediaError as error:
            reason = str(error)
        except (JobControl, BudgetExceeded, asyncio.CancelledError):
            # Shared checkpoint still identifies this asset; file sidecars preserve partial bytes.
            raise
        for file in files:
            file["path"] = str(folder / file["filename"])
        complete = reason is None
        payload = {"asset_id": asset["id"], "media_kind": candidate["kind"], "provenance": provenance,
            "files": files, "status": "complete" if complete else "partial", "media_complete": complete,
            "reason": reason, "quality": candidate.get("quality"), "available_qualities": candidate.get("available_qualities"),
            "source_index": candidate.get("index"), "format": candidate.get("format"),
            "segment_index": candidate.get("segment_index"), "segment_count": candidate.get("segment_count")}
        if asset.get("segments"):
            payload["segment_index"] = None
            payload["segments"] = [{"asset_id": item["id"], "candidate": item["candidate"],
                "provenance": item["provenance"]} for item in asset["segments"]]
        record = Record(provenance["platform"], "media_asset", context.job_id + ":" + asset["id"],
            provenance.get("source_url", candidate["url"]), payload,
            source_date=provenance.get("source_date"), source_date_field=provenance.get("source_date_field"),
            coverage={"complete": complete, "source_record_key": provenance.get("record_key"), "reason": reason})
        results.append({"asset_id": asset["id"], "complete": complete, "reason": reason})
        asset["result"] = payload
        atomic_json(manifest_path, manifest)
        checkpoint = await context.commit_page("media", [record], next_cursor={"index": index + 1}, exhausted=False,
            expected_revision=revision, metadata={"manifest": "media/manifest.json", "candidates": len(assets)})
        revision = checkpoint["revision"]
    complete = bool(assets) and all(item["complete"] for item in results)
    summary = {"status": "complete" if complete else "partial", "coverage_complete": complete,
        "candidates": len(assets), "assets_complete": sum(item["complete"] for item in results),
        "files": len(budget.files()), "retained_bytes": maximum_bytes - budget.remaining(),
        "reason": None if complete else "no_media_candidates" if not assets else "media_incomplete",
        "results": results, "manifest": str(manifest_path), "elapsed_seconds": round(time.monotonic() - started, 3)}
    network = getattr(context, "_network_budget", None)
    if network:
        summary.update(network_bytes=network.bytes, requests=network.requests)
    await context.commit_page("media", [], next_cursor=None, exhausted=complete, expected_revision=revision,
        metadata={"manifest": "media/manifest.json", "summary": dict(summary)})
    return summary
