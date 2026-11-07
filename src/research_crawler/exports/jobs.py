"""Freeze one committed Store view, then checkpoint each verified export format."""
from __future__ import annotations

import asyncio
import json
import time

from ..core.records import Record, canonical_json, utcnow
from .writers import WRITERS, artifact, atomic, tables

CAPABILITIES = {"implemented": ["json", "jsonl", "csv", "excel", "sqlite_snapshot", "sqlite_native",
    "source_kind_tables", "all_observed_versions", "atomic_files", "hash_checked_resume"],
    "handler": "export.records", "source": "shared_store", "remote_connections": False,
    "upstream_sqlite_tables": 15,
    "optional_dependencies": {"sqlite_native": "sqlalchemy[asyncio] (databases extra)"},
    "not_implemented": ["mysql_target", "postgresql_target", "mongodb_target"]}


async def settled_work(function, *args):
    """A cancelled Python thread still writes: settle it before job release/resume."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


def freeze(store, source_id, path, max_records, max_bytes):
    metadata = {}
    def write(temp):
        # One read transaction freezes source status and observation versions.
        # Never copy the complete application DB (accounts, events, unrelated jobs).
        with store.connection() as db, temp.open("wb") as output:
            db.execute("BEGIN")
            source = db.execute("SELECT kind,status,summary_json FROM jobs WHERE id=?", (source_id,)).fetchone()
            if source is None or source["status"] in {"pending", "running"}:
                raise ValueError("Select an existing settled source job")
            available = db.execute("SELECT COUNT(*) FROM observations WHERE job_id=?", (source_id,)).fetchone()[0]
            cursor = db.execute("""SELECT o.*,r.source,r.kind,r.native_id,v.payload_json,
                v.source_date,v.source_date_field FROM observations o
                JOIN records r ON r.key=o.record_key
                JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
                WHERE o.job_id=? ORDER BY o.record_key,o.content_hash""", (source_id,))
            count = size = 0
            for item in cursor:
                row = dict(item)
                row["payload"] = json.loads(row.pop("payload_json"))
                row["coverage"] = json.loads(row.pop("coverage_json"))
                encoded = (canonical_json(row) + "\n").encode("utf-8")
                if count >= max_records or size + len(encoded) > max_bytes:
                    break
                output.write(encoded)
                count += 1
                size += len(encoded)
            metadata.update(schema="research-crawler.export.v1", source_job_id=source_id,
                source_kind=source["kind"], source_status=source["status"],
                source_coverage_complete=json.loads(source["summary_json"]).get("coverage_complete") is True,
                snapshot_at=utcnow(), available_versions=available, selected_versions=count,
                selection_limited=count < available, version_policy="all_observed_in_source_job",
                ordering="record_key,content_hash", max_records=max_records, max_bytes=max_bytes)
    atomic(path, write)
    metadata["snapshot_sha256"] = artifact(path, "application/x-ndjson")["sha256"]
    metadata["tables"] = tables(path)
    metadata["format_notes"] = {
        "jsonl": "Lossless observation envelopes including nested payload, dates and coverage; always included.",
        "csv": "ZIP of source/kind tables and manifest; nested values JSON; formula-like strings apostrophe escaped; null and missing cells blank. Companion JSONL retains exact types.",
        "excel": "Upstream styles and row formatting; stable union headers; nested values JSON; oversized or XML-invalid text preserved as JSON string chunks in Overflow. Companion JSONL retains exact types.",
        "sqlite": "Source/kind tables retain payload columns plus prefixed metadata; record_envelopes preserves exact JSON types. This is an export schema, not an upstream ORM database backup.",
        "sqlite_native": "Original MediaCrawler 15-table ORM schema plus rc_export_* evidence sidecars; all selected observation envelopes survive; native rows follow natural-ID projection. Job-owned file only, no existing user database modified."}
    return metadata


def verified(item):
    from pathlib import Path
    path = Path(item["path"])
    return path.is_file() and artifact(path, item["content_type"]) == item


async def export_records(context):
    started = time.perf_counter()
    params = context.params
    source_id = params.get("source_job_id")
    formats = params.get("formats", ["jsonl"])
    if not isinstance(source_id, str) or not source_id or source_id == context.job_id:
        raise ValueError("Supply source_job_id")
    if not isinstance(formats, list) or not formats or any(not isinstance(f, str) for f in formats):
        raise ValueError("formats must be a list")
    formats = ["excel" if f == "xlsx" else f for f in formats]
    if len(set(formats)) != len(formats) or set(formats) - {*WRITERS, "jsonl", "sqlite_native"}:
        raise ValueError("Supported formats: json/jsonl/csv/excel/sqlite/sqlite_native")
    maximum, byte_budget = params.get("max_records", 100000), params.get("max_bytes", 268435456)
    if type(maximum) is not int or not 1 <= maximum <= 1000000 or type(byte_budget) is not int or not 1024 <= byte_budget <= 1073741824:
        raise ValueError("Invalid snapshot record/byte budget")
    if any(key in params for key in ("target", "dsn", "connection_string", "output_path")):
        raise ValueError("Exports are local job-owned files; remote targets are not implemented")
    folder = context.paths.run(context.job_id)
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / "records.snapshot.jsonl"
    checkpoint = await context.checkpoint("export.records")
    files = checkpoint["metadata"]["files"] if checkpoint else []
    if checkpoint:
        manifest = checkpoint["metadata"]["manifest"]
        saved = next(item for item in files if item["path"] == str(snapshot.resolve()))
        if not await settled_work(verified, saved):
            # A changed/missing frozen input must not silently select newer source data.
            raise ValueError("Frozen export input is missing or changed; create a new export job")
    else:
        manifest = await settled_work(freeze, context.store, source_id, snapshot, maximum, byte_budget)
        files = [await settled_work(artifact, snapshot, "application/x-ndjson")]
        checkpoint = await context.commit_page("export.records", [], next_cursor="snapshot", exhausted=False,
            metadata={"manifest": manifest, "files": files})

    async def remember(item, step):
        nonlocal files, checkpoint
        files = [entry for entry in files if entry["path"] != item["path"]] + [item]
        checkpoint = await context.commit_page("export.records", [], next_cursor=step, exhausted=False,
            expected_revision=checkpoint["revision"], metadata={"manifest": manifest, "files": files})

    manifest_path = folder / "export-manifest.json"
    def write_manifest():
        atomic(manifest_path, lambda temp: temp.write_text(canonical_json(manifest) + "\n", encoding="utf-8"))
        return artifact(manifest_path, "application/json")
    await remember(await settled_work(write_manifest), "manifest")
    failures = []
    for format in formats:
        await context.check_control()
        if format == "jsonl":
            continue
        if format == "sqlite_native":
            from .sqlite_native import CONTENT_TYPE, FILENAME, export_native
            filename, content_type = FILENAME, CONTENT_TYPE
        else:
            writer, filename, content_type = WRITERS[format]
        path = folder / filename
        existing = next((item for item in files if item["path"] == str(path.resolve())), None)
        if existing and await settled_work(verified, existing):
            continue
        try:
            if format == "sqlite_native":
                item = await export_native(context, path, snapshot, manifest)
            else:
                await settled_work(writer, path, snapshot, manifest)
                item = await settled_work(artifact, path, content_type)
            await remember(item, format)
        except (ImportError, OSError, ValueError, OverflowError) as error:
            failures.append({"format": format, "reason": type(error).__name__})
            files = [item for item in files if item["path"] != str(path.resolve())]
    summary = {"status": "partial" if failures or manifest["selection_limited"] else "complete",
        "source_job_id": source_id, "source_status": manifest["source_status"], "files": files,
        "available_versions": manifest["available_versions"], "exported_versions": manifest["selected_versions"],
        "processing_complete": not failures, "selection_limited": manifest["selection_limited"],
        "coverage_complete": manifest["source_coverage_complete"] and not manifest["selection_limited"],
        "source_coverage_complete": manifest["source_coverage_complete"], "format_failures": failures,
        "snapshot_sha256": manifest["snapshot_sha256"], "version_policy": manifest["version_policy"],
        "artifact_bytes": sum(item["bytes"] for item in files),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    record = Record("derived_exports", "export", context.job_id, "", summary,
        coverage={"generated": True, "source_job_id": source_id, "snapshot_sha256": manifest["snapshot_sha256"]})
    await context.commit_page("export.records", [record], next_cursor=None, exhausted=not failures,
        expected_revision=checkpoint["revision"], metadata={"manifest": manifest, "files": files, "summary": dict(summary)})
    return summary
