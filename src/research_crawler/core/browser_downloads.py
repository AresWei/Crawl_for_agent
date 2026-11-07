"""Owned Chromium downloads integrated with the shared task/network lifecycle.

Filename confinement is adapted from Crawl4AI _safe_download_filepath and the
download handling contract from _handle_download, commit
838869bdda93f25e6d50b3f50ae9577d9b19a586. Original: UncleCode/Crawl4AI,
https://github.com/unclecode/crawl4ai. License: ../web/LICENSE.crawl4ai.
CDP progress budgets, GUID isolation, receipts and cleanup are local additions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .jobs import BudgetExceeded, JobControl


def safe_filename(filename):
    name = re.split(r"[/\\]", filename or "")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).replace("..", "_").strip(" .")[:140]
    if not name or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        name = "download_" + hashlib.sha256((filename or "").encode()).hexdigest()[:10]
    return name


class BrowserDownloads:
    def __init__(self, context, browser, browser_context, budget, *, owned_default_context=False):
        self.ctx, self.browser, self.context, self.budget = context, browser, browser_context, budget
        self.owned_default_context = owned_default_context
        self.folder = context.paths.run(context.job_id) / "downloads"
        self.receipt_path = self.folder / "download-receipts.json"
        # Chromium still encounters Windows filename limits for .crdownload;
        # keep the uniquely job-owned staging path shorter than the run output.
        self.temporary = context.paths.root / "temp" / ("downloads-" + uuid4().hex[:12])
        self.frames, self.requests, self.ledgers, self.entries = {}, {}, {}, {}
        self.page_cdps, self.tasks = [], set()
        self.watched_pages = set()
        self.page_download_counts, self.page_listeners = {}, {}
        self.cdp = self.context_id = self.monitor = None
        self.closed = False
        self.max_files = min(100, max(1, int(context.params.get("max_downloads", 20))))

    async def start(self, page):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.temporary.mkdir(parents=True, exist_ok=False)
        self.cdp = await self.browser.new_browser_cdp_session()
        page_cdp = await self.context.new_cdp_session(page)
        try:
            target = (await page_cdp.send("Target.getTargetInfo"))["targetInfo"]
            self.context_id = target.get("browserContextId")
        finally:
            await page_cdp.detach()
        if not self.context_id and not self.owned_default_context:
            raise ValueError("Downloads require an owned isolated browser context")
        self.cdp.on("Browser.downloadWillBegin", self._begin)
        self.cdp.on("Browser.downloadProgress", self._progress)
        await self.cdp.send("Browser.setDownloadBehavior", {"behavior": "allowAndName",
            **self.context_target(), "downloadPath": str(self.temporary.resolve()), "eventsEnabled": True})
        await self.watch_page(page)
        self.monitor = asyncio.create_task(self._monitor())
        return self

    def context_target(self):
        # Persistent Chromium owns its entire process and default context. CDP
        # requires omission (not null) to address that context. External CDP is
        # rejected by open_browser before constructing this download manager.
        return {"browserContextId": self.context_id} if self.context_id else {}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def watch_page(self, page):
        if page in self.watched_pages:
            return
        self.watched_pages.add(page)
        self.page_download_counts[page] = 0
        def expected(download):
            self.page_download_counts[page] += 1
        self.page_listeners[page] = expected
        page.on("download", expected)
        cdp = await self.context.new_cdp_session(page)
        self.page_cdps.append(cdp)
        def add_tree(tree):
            self.frames[tree["frame"]["id"]] = page
            for child in tree.get("childFrames", []):
                add_tree(child)
        await cdp.send("Page.enable")
        add_tree((await cdp.send("Page.getFrameTree"))["frameTree"])
        cdp.on("Page.frameAttached", lambda e: self.frames.__setitem__(e["frameId"], page))
        cdp.on("Page.frameNavigated", lambda e: self.frames.__setitem__(e["frame"]["id"], page))

    def request(self, page, event):
        key = (id(page), event["requestId"])
        previous = self.requests.get(key, {})
        urls = previous.get("urls", set()) | {event["request"]["url"]}
        self.requests[key] = {"frame": event.get("frameId"), "url": event["request"]["url"], "urls": urls, "guid": None}

    def network_received(self, page, request_id, total):
        key = (id(page), request_id)
        self._charge(key, network=total)

    def _charge(self, key, *, network=0, download=0):
        ledger = self.ledgers.setdefault(key, {"network": 0, "download": 0, "counted": 0})
        ledger["network"] = max(ledger["network"], int(network))
        ledger["download"] = max(ledger["download"], int(download))
        current = max(ledger["network"], ledger["download"])
        self.budget.bytes += current - ledger["counted"]
        ledger["counted"] = current
        if self.budget.bytes > self.budget.maximum_bytes:
            self.budget.reason = "network_byte_budget"

    def _begin(self, event):
        page = self.frames.get(event["frameId"])
        guid = event["guid"]
        if not re.fullmatch(r"[A-Za-z0-9-]+", guid):
            return
        if page is None:
            # Browser-level events carry no context ID. Scoped cancellation
            # cannot affect another context; never allow an untracked transfer.
            async def cancel_unknown():
                try:
                    await self.cdp.send("Browser.cancelDownload", {"guid": guid, **self.context_target()})
                except Exception:
                    pass
            self.spawn(cancel_unknown())
            return
        candidates = [(key, value) for key, value in self.requests.items()
                      if key[0] == id(page) and value["frame"] == event["frameId"]
                      and value["url"] == event["url"] and value["guid"] is None]
        key = candidates[-1][0] if candidates else ("download", guid)
        if candidates:
            candidates[-1][1]["guid"] = guid
        entry = {"guid": guid, "url": event["url"], "suggested_filename": event.get("suggestedFilename", ""),
            "filename": safe_filename(event.get("suggestedFilename", "")), "status": "downloading",
            "bytes": 0, "total_bytes": 0, "page": page, "ledger": key,
            "request_urls": sorted(candidates[-1][1]["urls"]) if candidates else [event["url"]]}
        self.entries[guid] = entry
        if len(self.entries) > self.max_files:
            self.budget.reason = "download_count_budget"
            self.spawn(self._cancel(entry, "download_count_budget"))

    def _progress(self, event):
        entry = self.entries.get(event["guid"])
        if entry is None or entry["status"] != "downloading":
            return
        entry["bytes"] = max(entry["bytes"], int(event.get("receivedBytes", 0)))
        entry["total_bytes"] = int(event.get("totalBytes", 0))
        self._charge(entry["ledger"], download=entry["bytes"])
        remaining = self.budget.maximum_bytes - self.budget.bytes
        if self.budget.reason or entry["total_bytes"] - entry["bytes"] > remaining:
            self.budget.reason = self.budget.reason or "network_byte_budget"
            self.spawn(self._cancel(entry, self.budget.reason))
        elif event["state"] == "completed":
            entry["status"] = "saving"
            self.spawn(self._publish(entry))
        elif event["state"] == "canceled":
            entry.update(status="failed", reason="browser_download_canceled")

    async def _cancel(self, entry, reason):
        if entry["status"] != "downloading":
            return
        entry.update(status="cancelled", reason=reason)
        try:
            await self.cdp.send("Browser.cancelDownload", {"guid": entry["guid"], **self.context_target()})
        except Exception:
            pass  # Closing the owned browser also terminates the transfer.

    async def _publish(self, entry):
        def publish():
            source = self.temporary / entry["guid"]
            target = self.folder / (entry["guid"] + "-" + entry["filename"])
            if source.is_symlink() or target.exists() or target.is_symlink() or target.resolve().parent != self.folder.resolve():
                raise ValueError("Unsafe browser download destination")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = source.stat().st_size
            source.replace(target)
            return str(target.resolve()), size, digest
        try:
            path, size, digest = await asyncio.to_thread(publish)
            entry.update(status="complete", path=path, bytes=size, sha256=digest)
        except Exception as error:
            entry.update(status="failed", reason=type(error).__name__)

    async def _monitor(self):
        while not self.closed:
            reason = self.budget.reason
            try:
                await self.ctx.check_control()
            except (JobControl, BudgetExceeded) as error:
                reason = getattr(error, "action", None) or getattr(error, "reason", "cancelled")
            for entry in list(self.entries.values()):
                if entry["status"] != "downloading":
                    continue
                # CDP progress can be sparse; inspect only this GUID's owned
                # partial file as a second incremental signal, never file data.
                for suffix in (".crdownload", ""):
                    path = self.temporary / (entry["guid"] + suffix)
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    entry["bytes"] = max(entry["bytes"], size)
                    self._charge(entry["ledger"], download=size)
                if reason or self.budget.reason:
                    await self._cancel(entry, reason or self.budget.reason)
            await asyncio.sleep(.05)

    def snapshot(self, page=None):
        return [{key: value for key, value in entry.items() if key not in {"page", "ledger"}}
                for entry in self.entries.values() if page is None or entry["page"] == page]

    async def wait_started(self, page, url, timeout=1.0, after=0):
        """Resolve navigation-aborted/download-event ordering without masking errors."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if any(item["page"] == page and url in item["request_urls"] for item in list(self.entries.values())[after:]):
                return True
            await self.ctx.check_control()
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(.01)

    async def settle(self, page):
        # Events emitted by the preceding JS command may arrive on the next turn.
        await asyncio.sleep(0)
        while (sum(item["page"] == page for item in self.entries.values()) < self.page_download_counts.get(page, 0)
               or any(item["status"] in {"downloading", "saving"} for item in self.entries.values() if item["page"] == page)):
            await self.ctx.check_control()
            await asyncio.sleep(.05)
        await self.ctx.check_control()
        return self.snapshot(page)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        for entry in self.entries.values():
            await self._cancel(entry, "task_closed")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.cdp:
            self.cdp.remove_listener("Browser.downloadWillBegin", self._begin)
            self.cdp.remove_listener("Browser.downloadProgress", self._progress)
        for page, listener in self.page_listeners.items():
            page.remove_listener("download", listener)
        for cdp in [*self.page_cdps, self.cdp]:
            if cdp:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        # Only GUID files within the newly-created owned directory are removed.
        for path in self.temporary.glob("*"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        self.temporary.rmdir()
        receipt = {"schema": "research-crawler.browser-downloads.v1", "job_id": self.ctx.job_id,
            "downloads": self.snapshot(), "network_bytes": self.budget.bytes,
            "budget_reason": self.budget.reason,
            "accounting": "max of correlated Network and download payload counters; progress/polling bounded, not a transport hard cap"}
        temporary = self.receipt_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.receipt_path)
