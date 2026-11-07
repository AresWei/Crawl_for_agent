"""Project-owned persistent Chromium profiles and same-job page TTL.

Adapted from Crawl4AI 838869b browser_profiler.py (shrink allowlists and
Default-folder selection) and browser_manager.py (last-use session TTL).
Apache-2.0 attribution/license remain in licenses/ and upstream/.
No user browser directory, global browser manager or portable credential export.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import shutil
import stat
import time

from .paths import safe_name


class ProfileBusy(RuntimeError):
    pass


# Source-derived allowlists; preserve all upstream shrink levels explicitly.
KEEP_PATTERNS = {
    "none": {"*"},
    "light": {"Network", "Cookies", "Local Storage", "Session Storage", "IndexedDB",
              "Preferences", "Secure Preferences", "Login Data", "Login Data For Account",
              "Web Data", "History", "History-journal", "Visited Links", "Bookmarks",
              "TransportSecurity", "Trust Tokens", "storage_state.json"},
    "medium": {"Network", "Cookies", "Local Storage", "Session Storage", "IndexedDB",
               "Preferences", "Secure Preferences", "Login Data", "Login Data For Account",
               "Web Data", "TransportSecurity", "storage_state.json"},
    "aggressive": {"Network", "Cookies", "Local Storage", "IndexedDB", "Preferences", "storage_state.json"},
    "minimal": {"Network", "Cookies", "Local Storage", "storage_state.json"},
}


def seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("TTL must be finite and positive")
    return value


def _no_link(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Browser profile paths must not contain links or junctions")


class BrowserProfileStore:
    """Named profiles restricted to this application home; no credential reads."""

    def __init__(self, paths):
        self.root = paths.root.absolute()
        if os.name == "nt" and self.root.drive.lower() != "f:":
            raise ValueError("Browser profiles must remain in the configured F-drive application home")
        self.base = self.root / "state" / "browser-profiles"
        self.locks = self.root / "state" / "browser-profile-locks"
        for folder in (self.base, self.locks):
            self._check(folder)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _check(self, path, *, recursive=False):
        path = Path(path).absolute()
        if not path.is_relative_to(self.root):
            raise ValueError("Browser profile path escaped application home")
        _no_link(self.root)
        cursor = path
        while cursor != self.root:
            _no_link(cursor)
            cursor = cursor.parent
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Browser profile path escaped application home")
        if recursive and path.is_dir():
            for parent, folders, files in os.walk(path, followlinks=False):
                for name in folders + files:
                    _no_link(Path(parent) / name)
        return path

    def _path(self, name):
        safe_name(name)
        if name.endswith((".", " ")) or name.split(".")[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("Reserved browser profile name")
        return self._check(self.base / name)

    @contextmanager
    def lock(self, name):
        self._path(name)
        target = self._check(self.locks / (name.lower() + ".lock"))
        handle = target.open("a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ProfileBusy("Browser profile is already in use") from None
                raise
            yield
        finally:
            handle.close()  # OS releases locks on normal close and process death.

    def _read(self, folder):
        target = self._check(folder / "owner.json")
        meta = json.loads(target.read_text(encoding="utf-8"))
        if meta.get("schema") != 1 or meta.get("name", "").casefold() != folder.name.casefold():
            raise ValueError("Invalid browser profile ownership metadata")
        return meta

    def _write(self, folder, metadata):
        temporary = self._check(folder / "owner.json.tmp")
        target = self._check(folder / "owner.json")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    @contextmanager
    def acquire(self, name, *, platform="web", account=None, job_id=None):
        """Hold until Chromium is closed; immutable identity prevents account mixing."""
        owner = {"platform": safe_name(platform), "account": safe_name(account) if account else None}
        with self.lock(name):
            folder = self._path(name)
            if folder.exists():
                metadata = self._read(folder)
                if metadata["owner"] != owner:
                    raise ValueError("Browser profile belongs to a different platform/account")
            else:
                folder.mkdir(mode=0o700)
                metadata = {"schema": 1, "name": name, "owner": owner, "created_at": time.time()}
            data = self._check(folder / "data", recursive=True)
            data.mkdir(mode=0o700, exist_ok=True)
            metadata.update(last_used_at=time.time(), active_pid=os.getpid(), active_job=job_id)
            self._write(folder, metadata)
            try:
                yield data
            finally:
                metadata.update(last_used_at=time.time(), active_pid=None, active_job=None)
                self._write(folder, metadata)

    def list_profiles(self):
        result = []
        for folder in sorted(self.base.iterdir()):
            self._check(folder)
            if folder.is_dir():
                metadata = self._read(folder)
                try:
                    with self.lock(folder.name):
                        busy = False
                except ProfileBusy:
                    busy = True
                result.append({**metadata, "busy": busy})
        return result

    def delete(self, name):
        with self.lock(name):
            folder = self._path(name)
            if not folder.exists():
                return False
            self._read(folder)
            self._check(folder, recursive=True)
            shutil.rmtree(folder)
            return True

    def expire(self, max_idle_seconds):
        """Explicit maintenance, never automatic deletion of saved login state."""
        ttl, removed, busy = seconds(max_idle_seconds), [], []
        for candidate in self.list_profiles():
            name = candidate["name"]
            try:
                with self.lock(name):
                    folder = self._path(name)
                    metadata = self._read(folder)
                    if time.time() - metadata["last_used_at"] > ttl:
                        self._check(folder, recursive=True)
                        shutil.rmtree(folder)
                        removed.append(name)
            except ProfileBusy:
                busy.append(name)
        return {"removed": removed, "busy": busy}

    def shrink(self, name, level="light", *, dry_run=True):
        if level not in KEEP_PATTERNS:
            raise ValueError("Unknown profile shrink level")
        with self.lock(name):
            folder = self._path(name)
            self._read(folder)
            data = self._check(folder / "data", recursive=True)
            target = data / "Default" if (data / "Default").is_dir() else data
            removed, kept, total = [], [], 0
            for item in target.iterdir():
                if level == "none" or any(item.name.startswith(p) for p in KEEP_PATTERNS[level]):
                    kept.append(item.name)
                    continue
                total += sum(p.stat().st_size for p in item.rglob("*") if p.is_file()) if item.is_dir() else item.stat().st_size
                removed.append(item.name)
                if not dry_run:
                    self._check(item, recursive=True)
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
            return {"removed": removed, "kept": kept, "bytes_freed": 0 if dry_run else total,
                    "bytes_removable": total, "dry_run": dry_run, "level": level}


class SessionPageRegistry:
    """Source-derived last-use TTL. Holds only owned pages in one job/process.

    Cleanup is awaited on access; no orphan background task or external-page close.
    The browser context stays owned by the shared JobContext resource stack.
    """
    def __init__(self, ttl_seconds=1800, *, clock=time.monotonic):
        self.ttl, self.clock, self.pages = seconds(ttl_seconds), clock, {}

    async def cleanup(self):
        for name, (page, last_used) in list(self.pages.items()):
            if page.is_closed() or self.clock() - last_used > self.ttl:
                await self.kill(name)

    async def get(self, name):
        await self.cleanup()
        item = self.pages.get(name)
        if item:
            self.pages[name] = (item[0], self.clock())
            return item[0]
        return None

    async def put(self, name, page):
        if name in self.pages and self.pages[name][0] is not page:
            await self.kill(name)
        self.pages[name] = (page, self.clock())

    def touch(self, name):
        if name in self.pages:
            self.pages[name] = (self.pages[name][0], self.clock())

    async def kill(self, name):
        item = self.pages.pop(name, None)
        if item and not item[0].is_closed():
            await item[0].close()

    async def close(self):
        for name in list(self.pages):
            await self.kill(name)
