"""Supplied account/proxy pairs, selected once and owned by the shared job core.

No response-triggered identity fallback. Secrets stay in the private stores and
in a job-local snapshot; SQLite holds aliases, revision hashes and safe events.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import uuid
from contextlib import asynccontextmanager

from .accounts import AccountStore, ProxyStore, PLATFORM_SITES, read_private, write_private
from .exclusive import lease
from .jobs import BudgetExceeded, JobControl, NeedsLogin
from .paths import safe_name
from .records import canonical_json, utcnow
from .store import Store, StateConflict


STREAM = "account_pool_identity"


class PoolUnavailable(RuntimeError):
    """No configured eligible pair; requires user action, not another identity retry."""


class PoolIdentityChanged(StateConflict):
    """A pinned alias now represents different private state; start a new job."""


class AccountPoolStore:
    def __init__(self, paths, store=None):
        self.paths = paths
        self.store = store or Store(paths.database)
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_pools (
                    name TEXT PRIMARY KEY, platform TEXT NOT NULL, revision TEXT NOT NULL,
                    cursor INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_pool_members (
                    pool TEXT NOT NULL REFERENCES account_pools(name), account TEXT NOT NULL,
                    proxy_id TEXT NOT NULL, position INTEGER NOT NULL, state TEXT NOT NULL,
                    health TEXT NOT NULL DEFAULT 'unverified', last_job TEXT, last_at TEXT,
                    PRIMARY KEY(pool, account));
                CREATE TABLE IF NOT EXISTS account_pool_assignments (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), pool TEXT NOT NULL,
                    pool_revision TEXT NOT NULL, platform TEXT NOT NULL, account TEXT NOT NULL,
                    proxy_id TEXT NOT NULL, identity_hash TEXT NOT NULL, assigned_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS pool_assignment_members
                    ON account_pool_assignments(platform, account, proxy_id);
            """)

    def _key(self):
        folder = self.paths.root / "state/account-pools"
        with AccountStore._locked(folder):
            path = folder / "identity-key.private"
            if not path.exists():
                write_private(path, {"key": secrets.token_hex(32)})
            return bytes.fromhex(read_private(path)["key"])

    def _snapshot(self, platform, account, proxy_id):
        accounts = AccountStore(self.paths)
        with accounts._locked(self.paths.profile(platform, account)):
            info = accounts.info(platform, account)
            if info["status"] in {"not_logged_in", "revoked", "auth_required"}:
                raise NeedsLogin(platform, account)
            state = accounts.state(platform, account, required=True)
        proxy = ProxyStore(self.paths).get(proxy_id)
        private = {"platform": platform, "account": account, "proxy_id": proxy_id,
                   "account_state": state, "proxy_url": proxy,
                   "session_revision": info.get("session_revision", info.get("saved_at"))}
        digest = hmac.new(self._key(), canonical_json(private).encode(), hashlib.sha256).hexdigest()
        return private, digest

    def save(self, name, platform, members, *, expected_revision=None):
        safe_name(name)
        if platform not in PLATFORM_SITES:
            raise ValueError("Pool requires a supported account platform")
        if not isinstance(members, list) or not 1 <= len(members) <= 32:
            raise ValueError("Pool requires 1..32 supplied account/proxy pairs")
        checked = []
        for item in members:
            if not isinstance(item, dict) or set(item) != {"account", "proxy_id"}:
                raise ValueError("Pool members accept only account and proxy_id aliases")
            account, proxy = safe_name(item["account"]), safe_name(item["proxy_id"])
            self._snapshot(platform, account, proxy)
            checked.append((account, proxy))
        if len({a for a, _ in checked}) != len(checked):
            raise ValueError("Each account has exactly one proxy binding per pool")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM account_pools WHERE name=?", (name,)).fetchone()
            if (old is None and expected_revision is not None) or (old and old["revision"] != expected_revision):
                raise StateConflict("Pool revision changed")
            if db.execute("""SELECT 1 FROM account_pool_assignments a JOIN jobs j ON j.id=a.job_id
                    WHERE a.pool=? AND j.status NOT IN ('complete','cancelled') LIMIT 1""", (name,)).fetchone():
                raise StateConflict("Cancel or finish pinned jobs before changing pool membership")
            for account, proxy in checked:
                if db.execute("""SELECT 1 FROM account_pool_members m JOIN account_pools p ON p.name=m.pool
                    WHERE p.platform=? AND m.account=? AND m.proxy_id<>? AND m.pool<>? LIMIT 1""",
                    (platform, account, proxy, name)).fetchone():
                    raise ValueError("An account already has a different proxy binding in another pool")
            revision = uuid.uuid4().hex
            db.execute("""INSERT INTO account_pools VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                platform=excluded.platform,revision=excluded.revision,cursor=0,updated_at=excluded.updated_at""",
                       (name, platform, revision, 0, utcnow()))
            db.execute("DELETE FROM account_pool_members WHERE pool=?", (name,))
            db.executemany("""INSERT INTO account_pool_members(pool,account,proxy_id,position,state)
                VALUES(?,?,?,?,'enabled')""", [(name, a, p, n) for n, (a, p) in enumerate(checked)])
        return self.get(name)

    def get(self, name):
        safe_name(name)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM account_pools WHERE name=?", (name,)).fetchone()
            if row is None:
                raise KeyError(name)
            result = dict(row)
            result["members"] = [dict(m) for m in db.execute(
                "SELECT account,proxy_id,state,health,last_job,last_at FROM account_pool_members WHERE pool=? ORDER BY position", (name,))]
            for member in result["members"]:
                member["active_jobs"] = [r[0] for r in db.execute("""SELECT a.job_id FROM account_pool_assignments a
                    JOIN jobs j ON j.id=a.job_id WHERE j.status='running' AND
                    ((a.platform=? AND a.account=?) OR a.proxy_id=?)""",
                    (row["platform"], member["account"], member["proxy_id"]))]
        result["strategy"] = "round_robin_idle_pairs"
        result["health_scope"] = "last_job_outcome_not_account_validity_or_proxy_uptime"
        return result

    def list(self):
        with self.store.connection() as db:
            names = [row[0] for row in db.execute("SELECT name FROM account_pools ORDER BY name")]
        return [self.get(name) for name in names]

    def set_member_state(self, name, account, state, *, expected_revision):
        if state not in {"enabled", "paused"}:
            raise ValueError("Member state must be enabled or paused")
        if state == "enabled":
            pool = self.get(name)
            member = next((m for m in pool["members"] if m["account"] == account), None)
            if not member:
                raise KeyError(account)
            self._snapshot(pool["platform"], account, member["proxy_id"])
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM account_pools WHERE name=?", (name,)).fetchone()
            if not row or row[0] != expected_revision:
                raise StateConflict("Pool revision changed")
            changed = db.execute("UPDATE account_pool_members SET state=? WHERE pool=? AND account=?", (state, name, account))
            if not changed.rowcount:
                raise KeyError(account)
            db.execute("UPDATE account_pools SET revision=?,updated_at=? WHERE name=?", (uuid.uuid4().hex, utcnow(), name))
        return self.get(name)

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ("pool", "pool_revision", "platform", "account", "proxy_id", "assigned_at")}

    def reserve(self, job_id, name, platform):
        try:
            return self._reserve(job_id, name, platform)
        except NeedsLogin as error:
            # Persist known invalid state after the selection transaction rolled
            # back. Pin the attempted pair even though no network was allowed.
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                member = db.execute("""SELECT m.*,p.platform,p.revision FROM account_pool_members m
                    JOIN account_pools p ON p.name=m.pool WHERE m.pool=? AND m.account=? AND p.platform=?""",
                    (name, error.account, platform)).fetchone()
                if member:
                    db.execute("UPDATE account_pool_members SET state='attention',health='auth_required',last_job=?,last_at=? WHERE pool=? AND account=?",
                               (job_id, utcnow(), name, error.account))
                    receipt = {"pool": name, "pool_revision": member["revision"], "platform": platform,
                               "account": error.account, "proxy_id": member["proxy_id"], "assigned_at": utcnow()}
                    inserted = db.execute("INSERT OR IGNORE INTO account_pool_assignments VALUES(?,?,?,?,?,?,?,?)",
                        (job_id, name, member["revision"], platform, error.account, member["proxy_id"], "", receipt["assigned_at"]))
                    if inserted.rowcount:
                        db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)", (job_id, STREAM,
                            canonical_json(receipt), 0, 1, canonical_json({"identity_hash": "", "scope": "pair_pinned_no_network_identity_yet"}), utcnow()))
                    self.store._event(db, job_id, "account_pool_attention", {**receipt, "reason": "auth_required_before_network"})
            raise

    def _reserve(self, job_id, name, platform):
        """Short transaction reserves an idle pair and pins it before any network.

        None means busy. An unavailable or changed pinned pair never falls back.
        """
        safe_name(name)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job[0] != "running":
                raise StateConflict("Pool reservation requires a running owned job")
            pool = db.execute("SELECT * FROM account_pools WHERE name=?", (name,)).fetchone()
            if not pool or pool["platform"] != platform:
                raise ValueError("Pool does not belong to this platform")
            prior = db.execute("SELECT * FROM account_pool_assignments WHERE job_id=?", (job_id,)).fetchone()
            members = db.execute("SELECT * FROM account_pool_members WHERE pool=? ORDER BY position", (name,)).fetchall()
            if prior:
                if prior["pool"] != name or prior["platform"] != platform:
                    raise PoolIdentityChanged("Pinned pool changed")
                candidates = [m for m in members if m["account"] == prior["account"] and m["proxy_id"] == prior["proxy_id"]]
                if candidates and candidates[0]["state"] == "attention" and candidates[0]["health"] == "auth_required":
                    raise NeedsLogin(platform, prior["account"])
            else:
                offset = pool["cursor"] % len(members)
                candidates = [*members[offset:], *members[:offset]]
            candidates = [m for m in candidates if m["state"] == "enabled"]
            if not candidates:
                raise PoolUnavailable("No enabled member; user attention required")
            for member in candidates:
                busy = db.execute("""SELECT 1 FROM account_pool_assignments a JOIN jobs j ON j.id=a.job_id
                    WHERE j.status='running' AND a.job_id<>? AND
                    ((a.platform=? AND a.account=?) OR a.proxy_id=?) LIMIT 1""",
                    (job_id, platform, member["account"], member["proxy_id"])).fetchone()
                if busy:
                    continue
                # Snapshot only the selected candidate. Bad credentials stop this
                # job; they do not trigger an attempt with a different identity.
                private, digest = self._snapshot(platform, member["account"], member["proxy_id"])
                if prior:
                    if prior["identity_hash"] and prior["identity_hash"] != digest:
                        raise PoolIdentityChanged("Pinned private identity changed; start a new job")
                    if not prior["identity_hash"]:
                        # The earlier attempt stopped before any network/state
                        # snapshot. User re-enabled the same saved pair only.
                        db.execute("UPDATE account_pool_assignments SET identity_hash=? WHERE job_id=?", (digest, job_id))
                        db.execute("UPDATE checkpoints SET metadata_json=?,revision=revision+1,updated_at=? WHERE job_id=? AND stream=?",
                                   (canonical_json({"identity_hash": digest, "scope": "fixed_pair_for_entire_job"}), utcnow(), job_id, STREAM))
                    receipt = self._public(prior)
                else:
                    receipt = {"pool": name, "pool_revision": pool["revision"], "platform": platform,
                               "account": member["account"], "proxy_id": member["proxy_id"], "assigned_at": utcnow()}
                    db.execute("INSERT INTO account_pool_assignments VALUES(?,?,?,?,?,?,?,?)",
                               (job_id, name, pool["revision"], platform, member["account"], member["proxy_id"], digest, receipt["assigned_at"]))
                    db.execute("UPDATE account_pools SET cursor=? WHERE name=?", ((member["position"] + 1) % len(members), name))
                    db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)",
                               (job_id, STREAM, canonical_json(receipt), 0, 1,
                                canonical_json({"identity_hash": digest, "scope": "fixed_pair_for_entire_job"}), utcnow()))
                self.store._event(db, job_id, "account_pool_selected" if not prior else "account_pool_resumed", receipt)
                return receipt, private
        return None

    def finish(self, job_id, outcome):
        if outcome not in {"completed", "partial", "auth_required", "attention_required", "failed", "paused", "cancelled", "interrupted", "budget"}:
            raise ValueError("Unknown pool outcome")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM account_pool_assignments WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return
            db.execute("""UPDATE account_pool_members SET health=?,last_job=?,last_at=?,
                state=CASE WHEN ? IN ('auth_required','attention_required') THEN 'attention' ELSE state END
                WHERE pool=? AND account=?""", (outcome, job_id, utcnow(), outcome, row["pool"], row["account"]))
            self.store._event(db, job_id, "account_pool_outcome", {**self._public(row), "outcome": outcome})


def pinned_identity(context, platform, account):
    """Private in-memory resolver used only by shared HTTP/browser sessions."""
    value = getattr(context, "_pool_identity", None)
    if value is None:
        return None
    if value["platform"] != platform or value["account"] != account or value["proxy_id"] != context.params.get("proxy_id"):
        raise PoolIdentityChanged("Session requested a different pinned pool identity")
    return value


class FrozenAccounts(AccountStore):
    """Reuse the core CookieJar conversion without re-reading mutable aliases."""
    def __init__(self, identity):
        self.identity = identity

    def state(self, platform, account, *, required=False):
        if (platform, account) != (self.identity["platform"], self.identity["account"]):
            raise PoolIdentityChanged("Frozen account scope mismatch")
        return copy.deepcopy(self.identity["account_state"])


@asynccontextmanager
async def pooled_identity(context, platform):
    name = context.params.get("account_pool")
    if name is None:
        yield None
        return
    if not isinstance(name, str):
        raise ValueError("account_pool must be a registered pool alias")
    if any(context.params.get(key) is not None for key in ("account", "proxy_id", "proxy_policy", "cdp_url", "browser_profile", "session_id")):
        raise ValueError("Choose account_pool or explicit identity/browser settings, not both")
    pool = await asyncio.to_thread(AccountPoolStore, context.paths, context.store)
    while True:
        await context.check_control()
        # Settle this transaction even if cancellation arrives during SQLite I/O.
        task = asyncio.create_task(asyncio.to_thread(pool.reserve, context.job_id, name, platform))
        try:
            selected = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
        if selected is not None:
            break
        await asyncio.sleep(.1)
    receipt, private = selected
    outcome = "completed"
    try:
        await lease(context, "pool-account-leases", platform + ":" + receipt["account"])
        await lease(context, "pool-proxy-leases", receipt["proxy_id"])
        context._pool_identity = private
        context.resources.callback(lambda: context.__dict__.pop("_pool_identity", None))
        context.params.update(account=receipt["account"], proxy_id=receipt["proxy_id"])
        yield receipt
        if getattr(context, "_pool_outcome", None) == "partial":
            outcome = "partial"
    except NeedsLogin:
        outcome = "auth_required"
        raise
    except JobControl as error:
        outcome = "paused" if error.action == "pause" else "cancelled"
        raise
    except BudgetExceeded:
        outcome = "budget"
        raise
    except asyncio.CancelledError:
        outcome = "interrupted"
        raise
    except Exception as error:
        code = getattr(error, "code", None)
        status = getattr(getattr(error, "response", None), "status_code", None)
        outcome = "attention_required" if code in {"challenge_required", "captcha_required", "blocked", "http_403", "http_429"} or status in {401, 403, 429} else "failed"
        raise
    finally:
        task = asyncio.create_task(asyncio.to_thread(pool.finish, context.job_id, outcome))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
