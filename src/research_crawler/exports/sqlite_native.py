"""Job-owned MediaCrawler SQLite schema, with lossless observation sidecars.

Uses the retained original models and the shared SQL projection; never opens a
user-supplied database. A committed batch carries its own resume position in the
same SQLite transaction as its records, so a lost acknowledgement is harmless.
"""
from __future__ import annotations

from contextlib import closing
from itertools import islice
import json
import os
import sqlite3

from ..core.sessions import controlled
from .writers import artifact, rows


FILENAME = "records.native.sqlite3"
CONTENT_TYPE = "application/vnd.sqlite3"
BATCH_SIZE = 100


def _resume(path, job_id, manifest, snapshot, *, complete=False):
    from ..core.records import canonical_json
    from .remote import observation_id
    # Read-only validation precedes any SQLWriter initialization/mutation.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            receipt = db.execute("SELECT manifest_json FROM rc_export_manifests WHERE export_job_id=?", (job_id,)).fetchone()
            if receipt is None:
                raise ValueError("Native SQLite file has no matching export owner")
            saved = json.loads(receipt[0])
            if (saved.get("snapshot_sha256") != manifest["snapshot_sha256"]
                    or saved.get("source_job_id") != manifest["source_job_id"]):
                raise ValueError("Native SQLite snapshot changed; create a new export job")
            count = saved.get("delivered_versions")
            if type(count) is not int or not 0 <= count <= manifest["selected_versions"]:
                raise ValueError("Invalid native SQLite resume position")
            if db.execute("SELECT COUNT(*) FROM rc_export_observations").fetchone()[0] != count:
                raise ValueError("Native SQLite observation count changed")
            if complete and (count != manifest["selected_versions"] or saved.get("delivery_complete") is not True):
                raise ValueError("Final native SQLite export is not complete")
            # A matching receipt/count cannot establish that the frozen evidence
            # survived. Compare the committed prefix by immutable observation ID.
            with closing(rows(snapshot)) as source:
                compared = 0
                for row in islice(source, count):
                    observed = db.execute("SELECT envelope_json FROM rc_export_observations WHERE observation_id=?",
                                          (observation_id(row),)).fetchone()
                    if observed is None or observed[0] != canonical_json(row):
                        raise ValueError("Native SQLite observation content changed")
                    compared += 1
                if compared != count:
                    raise ValueError("Native SQLite resume position exceeds frozen input")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Native SQLite integrity check failed")
            return count
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise ValueError("Native SQLite file is not a valid owned export") from error


async def export_native(context, path, snapshot, manifest):
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import create_async_engine
    from .jobs import settled_work
    from .remote_sql import SQLWriter

    path = path.resolve()
    if path != context.paths.run(context.job_id).resolve() / FILENAME:
        raise ValueError("Native SQLite output must be the job-owned artifact")
    if path.exists():
        # Rename may have completed before its artifact/checkpoint acknowledgement.
        # Reuse only a complete owned export matching the frozen evidence. If a
        # prior checkpoint knows its hash, changing even a native projection is
        # grounds for rejection rather than silently blessing a replacement hash.
        saved = await context.checkpoint("export.records")
        previous = next((item for item in (saved or {}).get("metadata", {}).get("files", [])
                         if item["path"] == str(path)), None)
        current = await settled_work(artifact, path, CONTENT_TYPE)
        if previous is not None and previous != current:
            raise ValueError("Published native SQLite artifact changed")
        await settled_work(lambda: _resume(path, context.job_id, manifest, snapshot, complete=True))
        return current
    partial = path.with_name(path.name + ".partial")
    created = not partial.exists()
    if not created:
        count = await settled_work(_resume, partial, context.job_id, manifest, snapshot)
    else:
        # Exclusive creation prevents opening an unrelated existing database.
        with partial.open("xb"):
            pass
        count = 0

    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(partial)),
        echo=False, hide_parameters=True, connect_args={"timeout": 1}, pool_size=1, max_overflow=0)
    writer = SQLWriter(engine)
    source = rows(snapshot)
    established = not created
    try:
        async def initialize_owned():
            nonlocal established
            await writer.initialize()
            if created:
                await writer.write_batch([], {**manifest, "delivered_versions": 0}, context.job_id)
            established = True
        await controlled(context, initialize_owned())
        for _ in islice(source, count):
            pass
        while count < manifest["selected_versions"]:
            await context.check_control()
            batch = list(islice(source, BATCH_SIZE))
            if not batch:
                raise ValueError("Frozen native SQLite input ended early")
            count += len(batch)
            await controlled(context, writer.write_batch(batch,
                {**manifest, "delivered_versions": count}, context.job_id,
                complete=count == manifest["selected_versions"]))
        if count == 0:
            await controlled(context, writer.write_batch([], {**manifest, "delivered_versions": 0}, context.job_id, complete=True))
    finally:
        source.close()
        # Close before rename, including Windows file handles and worker threads.
        await writer.close()
        if created and not established:
            # Only this invocation's exclusive new file can be removed.
            partial.unlink(missing_ok=True)
    await context.check_control()
    await settled_work(_resume, partial, context.job_id, manifest, snapshot)
    def publish():
        if path.exists():
            raise ValueError("Native SQLite destination appeared before publication")
        with partial.open("r+b") as handle:
            os.fsync(handle.fileno())
        partial.replace(path)
        return artifact(path, CONTENT_TYPE)
    return await settled_work(publish)
