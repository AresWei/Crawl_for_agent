"""Job-scoped SQL adapters with original MediaCrawler schema projections.

Original models are copied with their license in remote_models.py. Additional
rc_export_* tables preserve every shared observation, including extra fields.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select, update
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import create_async_engine

from ..core.records import canonical_json
from . import remote_models
from .remote import digest, observation_id, order, route


metadata = MetaData()
large_text = Text().with_variant(LONGTEXT(), "mysql")
envelopes = Table("rc_export_observations", metadata,
    Column("observation_id", String(64), primary_key=True),
    Column("envelope_json", large_text, nullable=False))
manifests = Table("rc_export_manifests", metadata,
    Column("export_job_id", String(100), primary_key=True),
    Column("manifest_json", large_text, nullable=False))
projections = Table("rc_export_projections", metadata,
    Column("projection_id", String(64), primary_key=True),
    Column("observed_order", String(128), nullable=False))


def upsert(connection, table, values, *, immutable=False):
    dialect = connection.dialect.name
    if dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(table).values(**values)
        updates = {key: statement.inserted[key] for key in values if key not in table.primary_key.columns}
        if immutable:
            key = next(iter(table.primary_key.columns)).name
            updates = {key: table.c[key]}
        return statement.on_duplicate_key_update(**updates)
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":  # Real in-process relational fixture, not a public remote target.
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Unsupported relational target")
    statement = insert(table).values(**values)
    keys = [column.name for column in table.primary_key.columns]
    if immutable:
        return statement.on_conflict_do_nothing(index_elements=keys)
    return statement.on_conflict_do_update(index_elements=keys,
        set_={key: statement.excluded[key] for key in values if key not in keys})


def project_values(table, payload, identity_key, native_id):
    values = {}
    xhs_json_fields = {"xhs_note": {"image_list", "tag_list"},
                       "xhs_note_comment": {"pictures"}}.get(table.name, set())
    for column in table.columns:
        if column.name in xhs_json_fields:
            # Frozen XHS store calls json.dumps even for an already-normalized
            # comma-separated string, and json.dumps(None) when absent/null.
            values[column.name] = json.dumps(payload.get(column.name))
            continue
        if column.name == "id" or column.name not in payload:
            continue
        value = payload[column.name]
        if value is not None:
            if isinstance(column.type, Integer):
                value = int(value or 0)
            elif isinstance(value, (dict, list)):
                value = canonical_json(value)
            elif not isinstance(value, str):
                value = str(value)
        values[column.name] = value
    values[identity_key] = str(payload.get(identity_key) or native_id)
    return values


class SQLWriter:
    def __init__(self, engine, project_upstream=True):
        self.engine, self.project_upstream = engine, project_upstream

    @classmethod
    async def open(cls, settings):
        proactor = getattr(asyncio, "ProactorEventLoop", None)
        if (os.name == "nt" and settings.backend == "mysql" and proactor is not None
                and isinstance(asyncio.get_running_loop(), proactor)):
            from .remote_thread import SelectorSQLWriter
            return await SelectorSQLWriter.open(settings, cls._open_native)
        return await cls._open_native(settings)

    @classmethod
    async def _open_native(cls, settings):
        connect_args = {"connect_timeout": 10} if settings.backend == "mysql" else {"timeout": 10, "command_timeout": 30}
        engine = create_async_engine(settings.dsn, echo=False, hide_parameters=True,
            pool_size=1, max_overflow=0, pool_pre_ping=True, connect_args=connect_args)
        writer = cls(engine, settings.project_upstream)
        try:
            await writer.initialize()
            return writer
        except BaseException:
            await engine.dispose()
            raise

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            if self.project_upstream:
                await connection.run_sync(remote_models.Base.metadata.create_all)

    async def close(self):
        await self.engine.dispose()

    async def write_batch(self, batch, manifest, export_job_id, *, complete=False):
        projected = unsupported = 0
        async with self.engine.begin() as connection:
            # Serialize projections made by this adapter, including different
            # jobs, without adding constraints to the original platform tables.
            lock_id = digest("projection_lock")
            await connection.execute(upsert(connection, projections,
                {"projection_id": lock_id, "observed_order": ""}, immutable=True))
            await connection.execute(select(projections).where(projections.c.projection_id == lock_id).with_for_update())
            for row in batch:
                await connection.execute(upsert(connection, envelopes,
                    {"observation_id": observation_id(row), "envelope_json": canonical_json(row)}, immutable=True))
                mapping = route(row) if self.project_upstream else None
                if not mapping:
                    unsupported += 1
                    continue
                model_name, key, _ = mapping
                table = getattr(remote_models, model_name).__table__
                values = project_values(table, row["payload"], key, row["native_id"])
                identity = digest(table.name, values[key])
                previous = (await connection.execute(select(projections.c.observed_order).where(projections.c.projection_id == identity))).scalar_one_or_none()
                if previous is not None and previous >= order(row):
                    projected += 1
                    continue
                existing = (await connection.execute(select(table.c.id).where(table.c[key] == values[key]))).scalar_one_or_none()
                values["last_modify_ts"] = int(time.time() * 1000)
                if existing is None:
                    values.setdefault("add_ts", values["last_modify_ts"])
                    await connection.execute(table.insert().values(**values))
                else:
                    values.pop("add_ts", None)
                    # XHS original updates only mutable counts/timestamps.
                    if model_name in {"XhsNote", "XhsNoteComment"}:
                        fields = ({"last_modify_ts", "liked_count", "collected_count", "comment_count", "share_count", "last_update_time"}
                                  if model_name == "XhsNote" else {"last_modify_ts", "like_count", "sub_comment_count"})
                        values = {key: value for key, value in values.items() if key in fields}
                    await connection.execute(update(table).where(table.c.id == existing).values(**values))
                await connection.execute(upsert(connection, projections,
                    {"projection_id": identity, "observed_order": order(row)}))
                projected += 1
            receipt = {**manifest, "delivery_complete": complete}
            await connection.execute(upsert(connection, manifests,
                {"export_job_id": export_job_id, "manifest_json": canonical_json(receipt)}))
        return {"projected": projected, "envelope_only": unsupported}
