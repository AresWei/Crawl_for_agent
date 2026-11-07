"""Original Mongo natural-ID collections plus lossless observation envelopes."""
from __future__ import annotations

from ..core.records import canonical_json
from .remote import observation_id, order, route


def bson_value(value):
    """BSON's numeric range is narrower than the lossless JSON envelope."""
    if type(value) is int and not -(2**63) <= value < 2**63:
        return str(value)
    if isinstance(value, dict):
        return {key: bson_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [bson_value(item) for item in value]
    return value


class MongoWriter:
    def __init__(self, client, database, project_upstream=True):
        self.client, self.database, self.project_upstream = client, database, project_upstream
        self.indexed = set()

    @classmethod
    async def open(cls, settings):
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo.write_concern import WriteConcern
        client = AsyncIOMotorClient(settings.dsn, serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000, socketTimeoutMS=30000, maxPoolSize=1)
        try:
            await client.admin.command("ping")
            # Unacknowledged writes cannot support checkpoint receipts, even
            # when the supplied URI has w=0.
            database = client.get_database(settings.database, write_concern=WriteConcern(w="majority", wtimeout=30000))
            return cls(client, database, settings.project_upstream)
        except BaseException:
            client.close()
            raise

    async def close(self):
        self.client.close()

    async def write_batch(self, batch, manifest, export_job_id, *, complete=False):
        projected = unsupported = 0
        for row in batch:
            # String JSON preserves large JSON integers and keys forbidden by
            # older BSON servers; no extra payload fields disappear.
            await self.database.rc_export_observations.update_one(
                {"_id": observation_id(row)}, {"$setOnInsert": {"envelope_json": canonical_json(row)}}, upsert=True)
            mapping = route(row) if self.project_upstream else None
            if not mapping or mapping[2] is None:
                unsupported += 1
                continue
            _, key, name = mapping
            collection = self.database[name]
            # Native natural-ID uniqueness is essential for replay safety.
            if name not in self.indexed:
                await collection.create_index(key, unique=True)
                self.indexed.add(name)
            payload = bson_value(row["payload"])
            payload[key] = str(payload.get(key) or row["native_id"])
            payload.pop("_id", None)
            payload["_rc_observed_order"] = order(row)
            # A pipeline condition prevents an older version or late export
            # from overwriting a newer projection. $$ROOT merging retains
            # upstream $set semantics. MongoDB >=4.2 is required.
            await collection.update_one({key: payload[key]}, [{"$replaceWith": {"$cond": [
                {"$gt": [{"$literal": order(row)}, {"$ifNull": ["$_rc_observed_order", ""]}]},
                {"$mergeObjects": ["$$ROOT", {"$literal": payload}]}, "$$ROOT"]}}], upsert=True)
            projected += 1
        await self.database.rc_export_manifests.update_one({"_id": export_job_id},
            {"$set": {"manifest_json": canonical_json({**manifest, "delivery_complete": complete})}}, upsert=True)
        return {"projected": projected, "envelope_only": unsupported}
