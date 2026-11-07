"""Local stream-copy assembly with bounded output and actual FFprobe evidence."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil

from ..core.jobs import BudgetExceeded
from ..core.records import utcnow
from .manager import MediaError, atomic_json, digest, process, read_json


async def probe(context, executable, path):
    raw = await process(context, [executable, "-v", "error", "-protocol_whitelist", "file",
        "-count_packets", "-show_entries",
        "format=duration:stream=codec_type,codec_name,profile,width,height,pix_fmt,sample_rate,channels,channel_layout,nb_read_packets",
        "-of", "json", str(path)])
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
        if not math.isfinite(duration) or duration <= 0 or not streams:
            raise ValueError()
        if any(item.get("codec_type") not in {"audio", "video"} or not item.get("codec_name")
                or int(item["nb_read_packets"]) <= 0 for item in streams):
            raise ValueError()
        return {"duration_seconds": duration, "streams": streams}
    except (KeyError, ValueError, TypeError):
        raise MediaError("concat_integrity_unverifiable") from None


def signature(probed):
    return [{key: value for key, value in stream.items() if key != "nb_read_packets"}
        for stream in probed["streams"]]


async def concatenate(context, folder, budget, identity, files):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaError("ffmpeg_or_ffprobe_unavailable")
    output = folder / (identity + ".concat.mp4")
    temporary = folder / (identity + ".concat.part.mp4")
    listing = folder / (identity + ".concat.tmp")
    state_path = folder / (identity + ".concat.json")
    inputs = [{key: item[key] for key in ("sha256", "size", "segment_index", "source_url", "download_url")}
        for item in files]
    state = read_json(state_path)
    if (output.is_file() and state.get("inputs") == inputs and state.get("size") == output.stat().st_size
            and state.get("sha256") == await asyncio.to_thread(digest, output)):
        await context.check_control()
        return state
    output.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    budget.allocate(temporary)
    available = budget.remaining()
    if available <= 0:
        raise BudgetExceeded("media_byte_budget")
    # Only manager-generated relative basenames can enter the concat script.
    if any(not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", item["filename"]) for item in files):
        raise MediaError("concat_invalid_local_input")
    originals = [await probe(context, ffprobe, folder / item["filename"]) for item in files]
    if any(signature(item) != signature(originals[0]) for item in originals[1:]):
        raise MediaError("concat_stream_mismatch")
    if not any(stream["codec_type"] == "video" for stream in originals[0]["streams"]):
        raise MediaError("concat_video_stream_missing")
    expected_duration = sum(item["duration_seconds"] for item in originals)
    expected_packets = [sum(int(item["streams"][index]["nb_read_packets"]) for item in originals)
        for index in range(len(originals[0]["streams"]))]
    try:
        listing.write_text("ffconcat version 1.0\n" + "".join(f"file '{item['filename']}'\n" for item in files), encoding="utf-8")
        await process(context, [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
            "-protocol_whitelist", "file", "-f", "concat", "-safe", "1", "-i", str(listing),
            "-map", "0", "-c", "copy", "-fs", str(available), str(temporary)])
        if not temporary.is_file() or temporary.stat().st_size > available:
            raise BudgetExceeded("media_byte_budget")
        actual = await probe(context, ffprobe, temporary)
        if signature(actual) != signature(originals[0]):
            raise MediaError("concat_stream_mismatch")
        if [int(item["nb_read_packets"]) for item in actual["streams"]] != expected_packets:
            raise MediaError("concat_packet_count_mismatch")
        # Container timestamp/encoder padding may round by milliseconds; a lost
        # segment or a materially truncated timeline must never pass this check.
        tolerance = max(0.1, 0.05 * len(files))
        if abs(actual["duration_seconds"] - expected_duration) > tolerance:
            raise MediaError("concat_duration_mismatch")
        actual_hash = await asyncio.to_thread(digest, temporary)
        await context.check_control()
        os.replace(temporary, output)
        state = {"filename": output.name, "size": output.stat().st_size, "sha256": actual_hash,
            "content_type": "video/mp4", "completed_at": utcnow(), "inputs": inputs,
            "verification": {**actual, "expected_duration_seconds": expected_duration,
                "duration_tolerance_seconds": tolerance, "expected_packet_counts": expected_packets}}
        atomic_json(state_path, state)
        return state
    finally:
        temporary.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
