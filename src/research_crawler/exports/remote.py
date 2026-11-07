"""Optional target configuration and source-derived storage routing.

No remote destination or credential is persisted in a job or receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ..core.accounts import read_private, write_private
from ..core.paths import safe_name
from ..core.records import canonical_json


CAPABILITIES = {"handler": "export.remote", "backends": ["mysql", "postgresql", "mongodb"],
    "private_named_targets": True, "all_observed_versions": True,
    "upstream_sql_tables": 15,
    "live_verified": {"mysql": {"version": "8.4.13", "environment": "Windows local TLS; default Proactor with owned Selector writer", "artifact": "artifacts/mysql-live.json"},
                      "postgresql": {"version": "18.6", "environment": "Windows local SCRAM server", "artifact": "artifacts/postgres-live.json"},
                      "mongodb": {"version": "8.0.34", "environment": "Windows authenticated standalone", "artifact": "artifacts/mongo-live.json"}},
    "acceptance_scope": "Bounded fixture and copied public records; no arbitrary-server, production-scale or replica-durability claim"}


def validate_params(params):
    allowed = {"source_job_id", "target_name", "max_records", "max_bytes", "batch_size", "max_seconds"}
    if not isinstance(params, dict) or set(params) - allowed:
        raise ValueError("Remote export jobs accept a named private target, never connection credentials")
    for key in ("source_job_id", "target_name"):
        if not isinstance(params.get(key), str):
            raise ValueError("Supply source_job_id and target_name")
        safe_name(params[key])
    for key, default, lower, upper in (("max_records", 100000, 1, 1000000),
            ("max_bytes", 268435456, 1024, 1073741824), ("batch_size", 100, 1, 1000)):
        value = params.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Invalid remote export budget")
    if "max_seconds" in params and (type(params["max_seconds"]) not in (int, float) or not 1 <= params["max_seconds"] <= 7200):
        raise ValueError("Invalid remote export time budget")


class RemoteTargetStore:
    def __init__(self, paths):
        self.folder = paths.root / "state" / "export-targets"

    def _path(self, name):
        return self.folder / (safe_name(name) + ".private")

    @staticmethod
    def public(value):
        return {key: value[key] for key in ("name", "backend", "project_upstream", "revision")}

    def save(self, name, backend, dsn, *, database=None, project_upstream=True):
        path = self._path(name)
        backend = {"db": "mysql", "postgres": "postgresql"}.get(backend, backend)
        if backend not in {"mysql", "postgresql", "mongodb"} or type(project_upstream) is not bool:
            raise ValueError("Expected mysql/postgresql/mongodb and boolean project_upstream")
        if not isinstance(dsn, str) or len(dsn) > 8192:
            raise ValueError("Invalid private connection configuration")
        try:
            parsed = urlsplit(dsn)
            valid = bool(parsed.hostname)
            parsed.port
        except (ValueError, TypeError):
            valid = False
        schemes = {"mysql": {"mysql", "mysql+asyncmy"},
                   "postgresql": {"postgresql", "postgresql+asyncpg"},
                   "mongodb": {"mongodb", "mongodb+srv"}}
        if not valid or parsed.scheme not in schemes[backend] or parsed.fragment:
            raise ValueError("Invalid private connection configuration")
        if backend != "mongodb":
            if not parsed.path.strip("/"):
                raise ValueError("Connection must name an existing database")
            if "+" not in parsed.scheme:
                dsn = dsn.replace(parsed.scheme + ":", {"mysql": "mysql+asyncmy:", "postgresql": "postgresql+asyncpg:"}[backend], 1)
        elif not isinstance(database, str) or not database or any(c in database for c in '/\\. "$*<>:|?\x00'):
            raise ValueError("MongoDB requires an explicit valid database name")
        value = {"name": name, "backend": backend, "dsn": dsn, "database": database,
                 "project_upstream": project_upstream, "revision": uuid4().hex}
        write_private(path, value)
        return self.public(value)

    def get(self, name):
        return read_private(self._path(name))

    def list(self):
        return [self.public(read_private(path)) for path in sorted(self.folder.glob("*.private"))]

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)


# Frozen MediaCrawler database/models.py and store/*/_store_impl.py.
# All creators/contacts are deliberately absent from that source's schema.
ROUTES = {
    ("bili", "video"): ("BilibiliVideo", "video_id", "bilibili_contents"),
    ("bili", "comment"): ("BilibiliVideoComment", "comment_id", "bilibili_comments"),
    ("bili", "dynamic"): ("BilibiliUpDynamic", "dynamic_id", None),
    ("dy", "video"): ("DouyinAweme", "aweme_id", "douyin_contents"),
    ("dy", "comment"): ("DouyinAwemeComment", "comment_id", "douyin_comments"),
    ("ks", "video"): ("KuaishouVideo", "video_id", "kuaishou_contents"),
    ("ks", "comment"): ("KuaishouVideoComment", "comment_id", "kuaishou_comments"),
    ("wb", "note"): ("WeiboNote", "note_id", "weibo_contents"),
    ("wb", "comment"): ("WeiboNoteComment", "comment_id", "weibo_comments"),
    ("xhs", "note"): ("XhsNote", "note_id", "xhs_contents"),
    ("xhs", "comment"): ("XhsNoteComment", "comment_id", "xhs_comments"),
    ("tieba", "note"): ("TiebaNote", "note_id", "tieba_contents"),
    ("tieba", "comment"): ("TiebaComment", "comment_id", "tieba_comments"),
    ("zhihu", "comment"): ("ZhihuComment", "comment_id", "zhihu_comments"),
    **{("zhihu", kind): ("ZhihuContent", "content_id", "zhihu_contents") for kind in ("answer", "article", "zvideo", "content")},
}
ALIASES = {"bilibili": "bili", "douyin": "dy", "kuaishou": "ks", "weibo": "wb", "xiaohongshu": "xhs"}


def route(row):
    return ROUTES.get((ALIASES.get(row["source"], row["source"]), row["kind"]))


def digest(*values):
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


def observation_id(row):
    # A resumed source can update coverage/URL/observed_at for the same content
    # hash. Different frozen envelopes must both survive subsequent exports.
    return digest(row)


def order(row):
    return row["observed_at"] + ":" + row["content_hash"]


@dataclass(repr=False)
class RemoteSettings:
    backend: str
    dsn: str = field(repr=False)
    database: str | None = None
    project_upstream: bool = True


async def open_writer(target):
    settings = RemoteSettings(**{key: target[key] for key in ("backend", "dsn", "database", "project_upstream")})
    if settings.backend == "mongodb":
        from .remote_mongo import MongoWriter
        return await MongoWriter.open(settings)
    from .remote_sql import SQLWriter
    return await SQLWriter.open(settings)
