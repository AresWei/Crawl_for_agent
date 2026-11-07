from __future__ import annotations

import json
import os
import sqlite3
import uuid
import psutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .records import Record, canonical_json, utcnow


TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"complete", "partial", "auth_required", "failed", "paused", "cancelled", "interrupted"},
    "paused": {"pending", "cancelled"}, "interrupted": {"pending", "cancelled"},
    "auth_required": {"pending", "cancelled"}, "failed": {"pending", "cancelled"},
    "partial": {"pending", "cancelled"}, "complete": set(), "cancelled": set(),
}


class StateConflict(RuntimeError):
    pass


def platform_resume_limits(job: dict) -> dict | None:
    """Read the actual platform cap without importing or running collectors."""
    if job["kind"] not in {"platform.collect", "platform.discover"}:
        return None
    params = job["params"]
    if job["kind"] == "platform.collect":
        effective = params.get("max_items", params.get("max_posts", 20))
        if params.get("platform") in {"xhs", "dy", "ks"}:
            effective = params.get("max_posts", effective)
    elif params.get("platform") == "stackexchange":
        effective = params.get("max_items", 100)
    else:
        effective = params.get("max_items", params.get("max_records", 20))
    try:
        previous = int(effective)
        declared = [int(params[key]) for key in ("max_items", "max_records", "max_posts") if key in params]
    except (ValueError, TypeError, OverflowError):
        return None
    if not 1 <= previous <= 1000 or any(not 1 <= value <= 1000 for value in declared):
        return None
    minimum = max([previous, *declared]) + 1
    return {"max_items": {"current": previous, "minimum": minimum, "maximum": 1000,
        "can_increase": minimum <= 1000 and "pending" in TRANSITIONS.get(job["status"], set())}}


class Store:
    """Short SQLite transactions; data and collection progress commit together.

    Network requests may repeat. Record versions and observations are idempotent;
    this is deliberately not an exactly-once claim about remote platforms.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'").fetchone()
            if existing:
                versions = db.execute("SELECT version FROM schema_info").fetchall()
                if len(versions) != 1 or versions[0][0] != 1:
                    raise RuntimeError("Unsupported database schema; do not modify this database")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
                INSERT INTO schema_info SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_info);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, params_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    requested_action TEXT, error_json TEXT, summary_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
                    at TEXT NOT NULL, type TEXT NOT NULL, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    key TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
                    native_id TEXT NOT NULL, url TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    UNIQUE(source, kind, native_id)
                );
                CREATE TABLE IF NOT EXISTS record_versions (
                    record_key TEXT NOT NULL REFERENCES records(key), content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, source_date TEXT, source_date_field TEXT,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(record_key, content_hash)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    job_id TEXT NOT NULL REFERENCES jobs(id), record_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL, observed_at TEXT NOT NULL, url TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    PRIMARY KEY(job_id, record_key, content_hash),
                    FOREIGN KEY(record_key, content_hash) REFERENCES record_versions(record_key, content_hash)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id), stream TEXT NOT NULL,
                    cursor_json TEXT NOT NULL, exhausted INTEGER NOT NULL,
                    revision INTEGER NOT NULL, metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, stream)
                );
                CREATE INDEX IF NOT EXISTS observation_jobs ON observations(job_id);
                CREATE INDEX IF NOT EXISTS event_jobs ON events(job_id, seq);
            """)
            if db.execute("SELECT version FROM schema_info").fetchone()[0] != 1:
                raise RuntimeError("Unsupported database schema; do not modify this database")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "worker_pid" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN worker_pid INTEGER")
            if "worker_started" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN worker_started REAL")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, job_id: str, kind: str, data: dict) -> None:
        db.execute("INSERT INTO events(job_id,at,type,data_json) VALUES(?,?,?,?)",
                   (job_id, utcnow(), kind, canonical_json(data)))

    def create_job(self, kind: str, params: dict[str, Any]) -> dict:
        if not isinstance(kind, str) or not kind.strip() or not isinstance(params, dict):
            raise ValueError("Job requires a kind and object parameters")
        # Pin the chosen default on creation, so UI, CLI and API agree while
        # already stored jobs keep their original execution semantics on resume.
        if kind in {"answer.model", "research.answer"}:
            params = dict(params)
            params.setdefault("answer_backend", "codex")
            if kind == "research.answer" and params["answer_backend"] == "codex":
                params.setdefault("evidence_selection", "context")
        job_id, now = uuid.uuid4().hex, utcnow()
        with self.connection() as db:
            db.execute("INSERT INTO jobs(id,kind,params_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (job_id, kind, canonical_json(params), "pending", now, now))
            self._event(db, job_id, "created", {"kind": kind})
        return self.job(job_id)

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict:
        result = dict(row)
        for name in ("params", "error", "summary"):
            value = result.pop(name + "_json")
            result[name] = json.loads(value) if value is not None else None
        return result

    def job(self, job_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return self._decode_job(row)

    def jobs(self, limit: int = 50) -> list[dict]:
        with self.connection() as db:
            return [self._decode_job(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def transition(self, job_id: str, target: str, *, expected: str | None = None,
                   error: dict | None = None, summary: dict | None = None) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,requested_action FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = row[0]
            if expected is not None and state != expected:
                raise StateConflict(f"Expected {expected}, got {state}")
            # Resolve control requests under the same write lock as completion.
            # A successful request must not be cleared by a later success commit.
            if state == "running" and target in {"complete", "partial"} and row["requested_action"]:
                target = "paused" if row["requested_action"] == "pause" else "cancelled"
            if target not in TRANSITIONS.get(state, set()):
                raise StateConflict(f"Cannot transition {state} to {target}")
            db.execute("""UPDATE jobs SET status=?,updated_at=?,requested_action=NULL,error_json=?,
                          summary_json=COALESCE(?,summary_json) WHERE id=?""",
                       (target, utcnow(), canonical_json(error) if error else None,
                        canonical_json(summary) if summary is not None else None, job_id))
            if target == "running":
                db.execute("UPDATE jobs SET worker_pid=?,worker_started=? WHERE id=?",
                           (os.getpid(), psutil.Process().create_time(), job_id))
            else:
                db.execute("UPDATE jobs SET worker_pid=NULL,worker_started=NULL WHERE id=?", (job_id,))
            self._event(db, job_id, "state", {"from": state, "to": target})
        return self.job(job_id)

    def recover_interrupted(self) -> list[str]:
        """Recover jobs whose actual owning process is gone, preserving other live workers.

        Creation time prevents a reused PID from being mistaken for the old worker.
        AccessDenied is unknown, not proof of death, so those jobs are left alone.
        """
        recovered = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,worker_pid,worker_started FROM jobs WHERE status='running'").fetchall():
                alive = False
                if row["worker_pid"] is not None and row["worker_started"] is not None:
                    try:
                        process = psutil.Process(row["worker_pid"])
                        alive = (abs(process.create_time() - row["worker_started"]) < .01
                                 and process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
                    except psutil.NoSuchProcess:
                        alive = False
                    except psutil.AccessDenied:
                        continue
                if alive:
                    continue
                db.execute("""UPDATE jobs SET status='interrupted',updated_at=?,requested_action=NULL,
                            worker_pid=NULL,worker_started=NULL,error_json=? WHERE id=?""",
                           (utcnow(), canonical_json({"code": "worker_lost"}), row["id"]))
                self._event(db, row["id"], "state", {"from": "running", "to": "interrupted", "reason": "worker_lost"})
                recovered.append(row["id"])
        return recovered

    def resume_job(self, job_id: str, *, max_items: int | None = None,
                   expected_status: str | None = None) -> dict:
        """Resume unchanged, or explicitly extend only a platform record budget.

        The state claim and alias updates share one transaction. Checkpoints,
        observations, source selection and identity parameters are untouched.
        """
        if max_items is not None and (type(max_items) is not int or not 1 <= max_items <= 1000):
            raise ValueError("max_items must be an integer from 1 to 1000")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = row["status"]
            if expected_status is not None and state != expected_status:
                raise StateConflict("Task state changed before resume")
            resumable = "pending" in TRANSITIONS.get(state, set())
            if not resumable and not (state == "pending" and max_items is None):
                raise StateConflict("Task must be stopped in a resumable state")
            params = json.loads(row["params_json"])
            if max_items is not None:
                if row["kind"] not in {"platform.collect", "platform.discover"}:
                    raise ValueError("Record budget extension is only available for platform tasks")
                limits = platform_resume_limits({"kind": row["kind"], "params": params, "status": state})
                if limits is None:
                    raise ValueError("Existing record budget is invalid")
                previous = limits["max_items"]["current"]
                if max_items < limits["max_items"]["minimum"]:
                    raise ValueError("max_items must increase the existing record budget")
                params["max_items"] = max_items
                if "max_records" in params:
                    params["max_records"] = max_items
                if row["kind"] == "platform.collect":
                    params["max_posts"] = max_items
                self._event(db, job_id, "budget_extended", {"old_max_items": previous, "new_max_items": max_items})
            db.execute("""UPDATE jobs SET params_json=?,status='pending',updated_at=?,requested_action=NULL,
                       error_json=NULL,worker_pid=NULL,worker_started=NULL WHERE id=?""",
                       (canonical_json(params), utcnow(), job_id))
            if state != "pending":
                self._event(db, job_id, "state", {"from": state, "to": "pending"})
            return self._decode_job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def request_action(self, job_id: str, action: str) -> dict:
        if action not in {"pause", "cancel"}:
            raise ValueError("Only pause or cancel may be requested")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] != "running":
                raise StateConflict("Control requests require a running job")
            db.execute("UPDATE jobs SET requested_action=?,updated_at=? WHERE id=?", (action, utcnow(), job_id))
            self._event(db, job_id, "control", {"action": action})
        return self.job(job_id)

    def checkpoint(self, job_id: str, stream: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE job_id=? AND stream=?", (job_id, stream)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["cursor"] = json.loads(result.pop("cursor_json"))
            result["metadata"] = json.loads(result.pop("metadata_json"))
            result["exhausted"] = bool(result["exhausted"])
            return result

    def commit_page(self, job_id: str, stream: str, records: Iterable[Record], *,
                    next_cursor: Any, exhausted: bool, expected_revision: int = 0,
                    metadata: dict | None = None) -> dict:
        batch = list(records)
        # Serialize before acquiring the write lock; an invalid item cannot advance a checkpoint.
        values = [(r, canonical_json(r.payload), canonical_json(r.coverage)) for r in batch]
        cursor_json, meta_json = canonical_json(next_cursor), canonical_json(metadata or {})
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job[0] != "running":
                raise StateConflict("Only a running job can commit results")
            previous = db.execute("SELECT revision FROM checkpoints WHERE job_id=? AND stream=?",
                                  (job_id, stream)).fetchone()
            actual_revision = previous[0] if previous else 0
            if actual_revision != expected_revision:
                raise StateConflict(f"Checkpoint revision changed: {actual_revision} != {expected_revision}")
            for record, payload_json, coverage_json in values:
                db.execute("""INSERT INTO records VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET url=excluded.url,last_seen=MAX(records.last_seen,excluded.last_seen)""",
                    (record.key, record.source, record.kind, record.native_id, record.url,
                     record.observed_at, record.observed_at))
                db.execute("""INSERT INTO record_versions VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(record_key,content_hash) DO UPDATE SET
                    last_seen=MAX(record_versions.last_seen,excluded.last_seen)""",
                    (record.key, record.content_hash, payload_json, record.source_date,
                     record.source_date_field, record.observed_at, record.observed_at))
                db.execute("""INSERT INTO observations VALUES(?,?,?,?,?,?)
                    ON CONFLICT(job_id,record_key,content_hash) DO UPDATE SET
                    observed_at=MAX(observations.observed_at,excluded.observed_at),
                    url=excluded.url,coverage_json=excluded.coverage_json""",
                    (job_id, record.key, record.content_hash, record.observed_at, record.url, coverage_json))
            revision = actual_revision + 1
            db.execute("""INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id,stream) DO UPDATE SET cursor_json=excluded.cursor_json,
                exhausted=excluded.exhausted,revision=excluded.revision,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (job_id, stream, cursor_json, int(exhausted), revision, meta_json, utcnow()))
            db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (utcnow(), job_id))
            self._event(db, job_id, "page_committed", {"stream": stream, "revision": revision,
                                                      "received": len(batch), "exhausted": exhausted})
        return self.checkpoint(job_id, stream)  # type: ignore[return-value]

    def observations(self, job_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("""SELECT o.*,r.source,r.kind,r.native_id,v.payload_json,
                v.source_date,v.source_date_field FROM observations o
                JOIN records r ON r.key=o.record_key
                JOIN record_versions v ON v.record_key=o.record_key AND v.content_hash=o.content_hash
                WHERE o.job_id=? ORDER BY o.observed_at,o.record_key LIMIT ? OFFSET ?""",
                (job_id, max(1, min(limit, 1000)), max(0, offset)))
            results = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                item["coverage"] = json.loads(item.pop("coverage_json"))
                results.append(item)
            return results

    def counts(self, job_id: str) -> dict[str, int]:
        with self.connection() as db:
            row = db.execute("""SELECT COUNT(*),COUNT(DISTINCT record_key)
                                FROM observations WHERE job_id=?""", (job_id,)).fetchone()
            return {"versions": row[0], "records": row[1]}

    def events(self, job_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.connection() as db:
            output = []
            for row in db.execute("SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                                  (job_id, after, max(1, min(limit, 1000)))):
                item = dict(row)
                item["data"] = json.loads(item.pop("data_json"))
                output.append(item)
            return output
