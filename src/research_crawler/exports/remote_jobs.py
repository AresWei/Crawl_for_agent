"""Remote exports owned by the same core job and frozen evidence lifecycle."""
from __future__ import annotations

import asyncio
from itertools import islice
import time

from ..core.records import Record, canonical_json
from ..core.sessions import controlled
from .jobs import freeze, settled_work, verified
from .remote import CAPABILITIES, RemoteTargetStore, open_writer, validate_params
from .writers import artifact, atomic, rows


async def export_remote(context):
    started = time.perf_counter()
    params = context.params
    validate_params(params)
    if params["source_job_id"] == context.job_id:
        raise ValueError("Select a separate settled source job")
    target = RemoteTargetStore(context.paths).get(params["target_name"])
    public_target = RemoteTargetStore.public(target)
    folder = context.paths.run(context.job_id)
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / "records.snapshot.jsonl"
    checkpoint = await context.checkpoint("export.remote")
    if checkpoint:
        metadata = checkpoint["metadata"]
        if metadata["target"] != public_target:
            raise ValueError("Target configuration changed; create a new export job")
        if not await settled_work(verified, metadata["snapshot"]):
            raise ValueError("Frozen export input is missing or changed; create a new export job")
        manifest = metadata["manifest"]
        count, projected, envelope_only = metadata["delivered"], metadata["projected"], metadata["envelope_only"]
    else:
        manifest = await settled_work(freeze, context.store, params["source_job_id"], snapshot,
            params.get("max_records", 100000), params.get("max_bytes", 268435456))
        count = projected = envelope_only = 0
        metadata = {"target": public_target, "snapshot": await settled_work(artifact, snapshot, "application/x-ndjson"),
                    "manifest": manifest, "delivered": 0, "projected": 0, "envelope_only": 0}
        checkpoint = await context.commit_page("export.remote", [], next_cursor=0, exhausted=False, metadata=metadata)

    # Driver import/connection occurs only after source validation. The core
    # owns closing on success, pause, cancellation, timeout and exceptions.
    async def open_owned_writer():
        writer = await open_writer(target)
        # Register ownership before controlled performs its completion-time
        # control check; a simultaneous pause must not discard the connection.
        context.resources.push_async_callback(writer.close)
        return writer

    writer = await controlled(context, open_owned_writer())
    source = rows(snapshot)
    try:
        for _ in islice(source, count):
            pass
        while True:
            await context.check_control()
            batch = list(islice(source, params.get("batch_size", 100)))
            if not batch and count != 0:
                break
            delivered = count + len(batch)
            complete = delivered == manifest["selected_versions"]
            delivery = {**manifest, "target": public_target, "delivered_versions": delivered}
            async with asyncio.timeout(90):
                result = await controlled(context,
                    writer.write_batch(batch, delivery, context.job_id, complete=complete))
            count = delivered
            projected += result["projected"]
            envelope_only += result["envelope_only"]
            metadata = {**metadata, "delivered": count, "projected": projected, "envelope_only": envelope_only}
            checkpoint = await context.commit_page("export.remote", [], next_cursor=count, exhausted=False,
                expected_revision=checkpoint["revision"], metadata=metadata)
            if complete:
                break
    finally:
        source.close()
    summary = {"status": "partial" if manifest["selection_limited"] else "complete",
        "source_job_id": params["source_job_id"], "source_status": manifest["source_status"],
        "target": public_target, "exported_versions": count, "projected_versions": projected,
        "envelope_only_versions": envelope_only, "selection_limited": manifest["selection_limited"],
        "coverage_complete": manifest["source_coverage_complete"] and not manifest["selection_limited"],
        "source_coverage_complete": manifest["source_coverage_complete"], "processing_complete": True,
        "snapshot_sha256": manifest["snapshot_sha256"], "version_policy": manifest["version_policy"],
        "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    receipt = folder / "remote-export-receipt.json"
    await settled_work(atomic, receipt,
        lambda temp: temp.write_text(canonical_json(summary) + "\n", encoding="utf-8"))
    summary["files"] = [metadata["snapshot"], await settled_work(artifact, receipt, "application/json")]
    record = Record("derived_exports", "remote_export", context.job_id, "", summary,
        coverage={"generated": True, "source_job_id": params["source_job_id"], "snapshot_sha256": manifest["snapshot_sha256"]})
    await context.commit_page("export.remote", [record], next_cursor=None, exhausted=True,
        expected_revision=checkpoint["revision"], metadata={**metadata, "summary": dict(summary)})
    return summary


def register(runner):
    runner.register("export.remote", export_remote)
