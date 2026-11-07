"""Source-derived Crawl4AI hook dispatch and bounded user JavaScript execution.

Derived from async_crawler_strategy.py at 838869bdda93f25e6d50b3f50ae9577d9b19a586
(Apache-2.0, unclecode/Crawl4AI). See hooks-source-manifest.json for changes.
Python callables are trusted in-process objects, never serialized/evaluated text.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import re

from playwright.async_api import Error as BrowserError

from ..core.jobs import BudgetExceeded, JobControl
from ..core.sessions import controlled

HOOK_NAMES = frozenset({"on_browser_created", "on_page_context_created", "on_user_agent_updated",
    "before_goto", "after_goto", "before_retrieve_html", "on_execution_started",
    "on_execution_ended", "before_return_html"})


class HookError(ValueError):
    """Safe error: no script, exception text, arguments or return values."""


class ScriptTimeout(ValueError):
    def __init__(self, summary):
        super().__init__("javascript_timeout")
        self.summary = summary


def deadline(params):
    value = float(params.get("script_timeout_seconds", params.get("timeout_seconds", 30)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("script_timeout_seconds must be finite and positive")
    return min(120, value)


def scripts(value):
    items = value if isinstance(value, list) else [value]
    if len(items) > 100:
        raise ValueError("JavaScript is limited to 100 steps per phase")
    result, size = [], 0
    for item in items:
        if isinstance(item, str):
            code, mode = item, "auto"
        elif isinstance(item, dict) and set(item) <= {"code", "mode"}:
            code, mode = item.get("code"), item.get("mode", "body")
        else:
            raise ValueError("JavaScript must be explicit code strings or code/mode objects")
        if not isinstance(code, str) or mode not in {"auto", "body", "expression", "function"}:
            raise ValueError("Invalid JavaScript code/mode")
        size += len(code.encode("utf-8"))
        if size > 1_000_000:
            raise ValueError("JavaScript exceeds one megabyte per phase")
        result.append((code, mode))
    return result


def expression(code, mode):
    # Compile explicit JS in its page realm. No Python eval or dynamic import.
    if mode == "auto":
        mode = "function" if re.match(r"^\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)", code) else "body"
    if mode == "function":
        return "async (hook) => await (" + code + ")(hook)"
    if mode == "expression":
        return "async (hook) => await (" + code + ")"
    return "async (hook) => {" + code + "\n}"


class BrowserHooks:
    def __init__(self, serialized=None):
        self.native, self.javascript, self.transforms = {}, {}, set()
        if serialized is not None and not isinstance(serialized, dict):
            raise ValueError("hooks must be a JavaScript hook mapping")
        for name, value in (serialized or {}).items():
            self._name(name)
            if name in {"on_browser_created", "on_user_agent_updated"}:
                raise ValueError(name + " requires a trusted Python hook")
            transform = isinstance(value, dict) and value.get("return_html") is True
            if isinstance(value, dict):
                if set(value) - {"code", "mode", "return_html"}:
                    raise ValueError("Invalid JavaScript hook options")
                value = {k: v for k, v in value.items() if k != "return_html"}
            parsed = scripts(value)
            if len(parsed) != 1:
                raise ValueError("Each hook accepts exactly one JavaScript script")
            if transform:
                if name != "before_return_html":
                    raise ValueError("return_html applies only to before_return_html")
                self.transforms.add(name)
            self.javascript[name] = parsed[0]

    @staticmethod
    def _name(name):
        if name not in HOOK_NAMES:
            raise ValueError("Unknown browser hook")

    @property
    def active(self):
        return bool(self.native or self.javascript)

    def set_hook(self, name, function, *, return_html=False):
        self._name(name)
        if not callable(function):
            raise ValueError("Native hooks require trusted callable objects")
        if return_html and name != "before_return_html":
            raise ValueError("return_html applies only to before_return_html")
        self.native[name] = function
        self.javascript.pop(name, None)
        self.transforms.discard(name)
        if return_html:
            self.transforms.add(name)

    async def execute(self, name, ctx, *args, adapter=None, **kwargs):
        if name not in self.native and name not in self.javascript:
            return None
        async def invoke():
            if name in self.native:
                value = self.native[name](*args, **kwargs)
                return await value if inspect.isawaitable(value) else value
            page = args[0] if args else kwargs["page"]
            # Only explicitly useful hook inputs enter the page. Python browser/context,
            # config and script return values can contain account secrets.
            data = {k: kwargs[k] for k in ("url", "html") if k in kwargs}
            evaluator = adapter.evaluate if adapter else lambda page, code, data: page.evaluate(code, data)
            return await evaluator(page, expression(*self.javascript[name]), data)
        try:
            return await controlled(ctx, asyncio.wait_for(invoke(), deadline(ctx.params)))
        except (JobControl, BudgetExceeded, asyncio.CancelledError):
            raise
        except Exception as error:
            reason = "timeout" if isinstance(error, TimeoutError) else "failed"
            raise HookError("browser_hook_" + reason + ":" + name) from None


async def execute_scripts(page, value, ctx, *, adapter=None):
    """Sequential source-style body execution plus explicit expression/function variants.

    Private `values` are available to trusted on_execution_ended only. `summary`
    contains no user results, source, exception messages or URLs.
    """
    outcomes, values = [], []
    browser_error = adapter.get_imports()[1] if adapter else BrowserError
    evaluator = adapter.evaluate if adapter else lambda page, code, data: page.evaluate(code, data)
    for code, mode in scripts(value):
        await ctx.check_control()
        async def run():
            await page.wait_for_load_state("domcontentloaded")
            try:
                result = await evaluator(page, expression(code, mode), {})
            except browser_error as error:
                if "Execution context was destroyed" not in str(error):
                    raise
                await page.wait_for_load_state("domcontentloaded")
                return None, "navigation"
            await page.wait_for_load_state("domcontentloaded")
            return result, "complete"
        try:
            result, status = await controlled(ctx, asyncio.wait_for(run(), deadline(ctx.params)))
            values.append(result)
            outcomes.append({"success": True, "status": status})
        except (JobControl, BudgetExceeded, asyncio.CancelledError):
            raise
        except Exception as error:
            values.append(None)
            outcomes.append({"success": False, "status": "timeout" if isinstance(error, TimeoutError) else "javascript_error"})
            if isinstance(error, TimeoutError):
                raise ScriptTimeout({"success": False, "steps": outcomes}) from None
    summary = {"success": all(item["success"] for item in outcomes), "steps": outcomes}
    return {"summary": summary, "values": values}


async def wait_for_javascript(page, condition, ctx, *, adapter, timeout):
    """Condition polling in the same main world as user code, including Patchright."""
    script = 'async () => { const value = (' + condition + '); return typeof value === "function" ? await value() : value; }'
    async def poll():
        while not await adapter.evaluate(page, script):
            await asyncio.sleep(.05)
    await controlled(ctx, asyncio.wait_for(poll(), timeout))
