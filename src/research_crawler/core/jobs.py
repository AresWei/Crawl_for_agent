from __future__ import annotations

import asyncio
import copy
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .paths import AppPaths
from .records import Record
from .store import Store


class NeedsLogin(Exception):
    def __init__(self, platform: str, account: str, reason: str = "Login required"):
        super().__init__(reason)
        self.platform, self.account = platform, account


class JobControl(Exception):
    def __init__(self, action: str):
        self.action = action


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.summary = None


@dataclass
class JobContext:
    job_id: str
    params: dict[str, Any]
    store: Store
    paths: AppPaths
    resources: AsyncExitStack
    answer_progress: dict | None = None

    def answer_budget_summary(self, reason: str) -> dict | None:
        """Snapshot already-settled answer progress without certifying completion."""
        if self.answer_progress is None:
            return None
        saved = copy.deepcopy(self.answer_progress)
        saved.update(status='partial',answer_status='budget_exceeded',insufficient=True,
            reason=reason,answer_backend=self.params.get('answer_backend','local'),
            account_quota_used=self.params.get('answer_backend')=='codex',
            limits='达到任务预算；已保存来源及已核对的部分回答。' if saved.get('claims') else
                   '达到任务预算；已保留来源与草稿记录，未发布未核对的模型结论。')
        return saved

    async def check_control(self) -> None:
        action = (await asyncio.to_thread(self.store.job, self.job_id))["requested_action"]
        if action:
            raise JobControl(action)
        budget = getattr(self, "_network_budget", None)
        if budget is not None and budget.reason:
            raise BudgetExceeded(budget.reason)

    async def checkpoint(self, stream: str) -> dict | None:
        await self.check_control()
        return await asyncio.to_thread(self.store.checkpoint, self.job_id, stream)

    async def commit_page(self, stream: str, records: list[Record], *, next_cursor: Any,
                          exhausted: bool, expected_revision: int = 0, metadata: dict | None = None) -> dict:
        # Finish the atomic data/checkpoint commit before honoring pause/cancel.
        result = await asyncio.to_thread(self.store.commit_page, self.job_id, stream, records,
            next_cursor=next_cursor, exhausted=exhausted, expected_revision=expected_revision, metadata=metadata)
        try:
            await self.check_control()
        except BudgetExceeded as error:
            # Preserve a handler's committed partial report, including provider
            # failures, when a latched budget stops further work after the commit.
            if metadata and isinstance(metadata.get("summary"), dict):
                error.summary = dict(metadata["summary"])
            raise
        return result


Handler = Callable[[JobContext], Awaitable[dict[str, Any]]]


class JobRunner:
    def __init__(self, store: Store, paths: AppPaths):
        self.store, self.paths = store, paths
        self.handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self.handlers:
            raise ValueError(f"Handler already registered: {kind}")
        self.handlers[kind] = handler

    async def run(self, job_id: str) -> dict:
        job = await asyncio.to_thread(self.store.job, job_id)
        handler = self.handlers.get(job["kind"])
        if handler is None:
            raise ValueError(f"No implementation registered for {job['kind']}; job was not started")
        # Cancelling to_thread does not stop its SQLite transaction. Settle the
        # claim first so a cancelled coroutine cannot leave a live-PID orphan.
        claim = asyncio.create_task(asyncio.to_thread(
            self.store.transition, job_id, "running", expected="pending"))
        try:
            await asyncio.shield(claim)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(claim)
            except Exception:
                # A failed claim does not own the job and must not change it.
                pass
            else:
                await asyncio.shield(asyncio.to_thread(
                    self.store.transition, job_id, "interrupted", expected="running"))
            raise
        try:
            self.paths.run(job_id).mkdir(parents=True, exist_ok=True)
            async with AsyncExitStack() as stack:
                context = JobContext(job_id, job["params"], self.store, self.paths, stack)
                await context.check_control()
                # Bound the entire handler, including a quiet response body or
                # browser/signing wait. Resource cleanup runs outside the limit.
                maximum_seconds = max(1, min(7200, float(job["params"].get("max_seconds", 1800))))
                deadline = asyncio.timeout(maximum_seconds)
                try:
                    async with deadline:
                        summary = await handler(context)
                except TimeoutError:
                    if deadline.expired():
                        raise BudgetExceeded("time_budget") from None
                    raise
                await context.check_control()
            await context.check_control()
            target = summary.pop("status", "complete")
            if target not in {"complete", "partial"}:
                raise ValueError("Handlers must finish as complete or partial, or raise a typed failure")
            return await asyncio.to_thread(self.store.transition, job_id, target, expected="running", summary=summary)
        except JobControl as control:
            target = "paused" if control.action == "pause" else "cancelled"
            return await asyncio.to_thread(self.store.transition, job_id, target, expected="running")
        except NeedsLogin as error:
            return await asyncio.to_thread(self.store.transition, job_id, "auth_required", expected="running",
                error={"code": "auth_required", "platform": error.platform, "account": error.account})
        except BudgetExceeded as error:
            if error.summary is None:
                # asyncio.timeout has cancelled and awaited the handler before
                # reaching here; its answer-progress finally has already run.
                error.summary = context.answer_budget_summary(error.reason)
            partial = dict(error.summary or {})
            partial.pop("status", None)
            partial.update(reason=error.reason, coverage_complete=False)
            return await asyncio.to_thread(self.store.transition, job_id, "partial", expected="running",
                summary=partial)
        except asyncio.CancelledError:
            # A worker interruption is not reported as a user cancellation or a successful task.
            await asyncio.shield(asyncio.to_thread(self.store.transition, job_id, "interrupted", expected="running"))
            raise
        except Exception as error:
            # Exception messages may contain upstream credentials or query strings: keep a safe class marker.
            return await asyncio.to_thread(self.store.transition, job_id, "failed", expected="running",
                error={"code": "handler_failed", "type": type(error).__name__})
