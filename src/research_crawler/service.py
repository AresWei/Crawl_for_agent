from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .core.jobs import JobRunner, NeedsLogin
from .core.paths import AppPaths
from .core.store import StateConflict, Store, platform_resume_limits
from .core.accounts import AccountStore, ProxyStore


class NewJob(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)


class NewAccount(BaseModel):
    platform: str
    account: str
    domains: list[str] | None = None


class NewProxy(BaseModel):
    name: str
    url: str


class PoolDefinition(BaseModel):
    platform: str
    members: list[dict]
    expected_revision: str | None = None


class PoolMemberState(BaseModel):
    state: str
    expected_revision: str


async def codex_model_health(root):
    """Account-login status only; never run a completion or use API fallback."""
    try:
        from .intelligence.codex_model import CodexModelClient
        async with CodexModelClient(root) as model:
            return await model.health()
    except (ImportError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        return {'available':False,'provider':'codex_chatgpt','status':'unavailable',
                'reason':type(error).__name__,'api_fallback':False}


def build_runner(store: Store, paths: AppPaths) -> tuple[JobRunner, dict]:
    runner = JobRunner(store, paths)
    modules = {}
    for name in ("web", "platforms", "search", "analysis", "accounts", "discovery", "media", "media_analysis", "comment_reports", "wordcloud", "exports", "seeding", "platform_discovery", "vision", "creation", "cache_transfer"):
        module_name = f"research_crawler.adapters.{name}"
        if importlib.util.find_spec(module_name) is None:
            modules[name] = {"status": "not_implemented"}
            continue
        try:
            module = importlib.import_module(module_name)
            module.register(runner)
            modules[name] = {"status": "registered", "capabilities": getattr(module, "CAPABILITIES", {})}
        except ImportError as error:
            modules[name] = {"status": "dependency_missing", "type": type(error).__name__}
    return runner, modules


def create_app(root: str | Path | None = None, *, runner_override: JobRunner | None = None) -> FastAPI:
    paths = AppPaths.discover(root)
    paths.initialize()
    store = Store(paths.database)
    if runner_override is None:
        runner, modules = build_runner(store, paths)
    else:
        runner, modules = runner_override, {"test": {"status": "registered"}}
    active: dict[str, asyncio.Task] = {}
    codex_health_cache = {'expires':0.0, 'value':None}
    codex_health_lock = asyncio.Lock()
    token_file = paths.root / "state" / "api-token"
    if not token_file.exists():
        try:
            with token_file.open("x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("Invalid local API token; refusing to expose the application")

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.recover_interrupted)
        yield
        for task in list(active.values()):
            task.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)

    app = FastAPI(title="Research Crawler", version=__version__, lifespan=lifespan)
    app.state.paths, app.state.store, app.state.runner = paths, store, runner
    ui = Path(__file__).parent / "ui"
    app.mount("/assets", StaticFiles(directory=ui), name="assets")

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "Local host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}" and not request.url.path.startswith('/bridge/v1/'):
            return JSONResponse({"detail": "Cross-origin access denied"}, status_code=403)
        if request.url.path.startswith("/api/"):
            provided = request.headers.get("x-research-token", "")
            if not secrets.compare_digest(provided, token):
                return JSONResponse({"detail": "Local API authentication required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request: Request, error: KeyError):
        return JSONResponse({"detail": "Task not found"}, status_code=404)

    @app.exception_handler(StateConflict)
    async def conflict(request: Request, error: StateConflict):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (ui / "index.html").read_text(encoding="utf-8").replace("__LOCAL_TOKEN__", token)

    @app.get("/api/status")
    async def status():
        model_status = {"available": False}
        if "answer.model" in runner.handlers:
            from .intelligence.local_model import LocalModelClient
            async with LocalModelClient(paths.root, timeout=3) as model:
                model_status = await model.health()
        codex_status = {'available':False,'status':'not_registered'}
        if any(kind in runner.handlers for kind in ('answer.model','research.answer')):
            # One login/status subprocess group per 30 seconds, even when
            # several UI/API callers poll concurrently. Negative checks cache too.
            async with codex_health_lock:
                if codex_health_cache['value'] is None or time.monotonic() >= codex_health_cache['expires']:
                    codex_health_cache['value'] = await codex_model_health(paths.root)
                    codex_health_cache['expires'] = time.monotonic()+30
                codex_status = dict(codex_health_cache['value'])
        return {"version": __version__, "home": str(paths.root), "modules": modules,
                "handlers": list(runner.handlers), "active": list(active),
                "model": model_status, "codex_model":codex_status,
                "acceptance": "in_progress", "paid_api_enabled": False}

    @app.get("/api/jobs")
    async def jobs():
        return {"jobs": store.jobs()}

    @app.get("/api/accounts")
    async def accounts():
        return {"accounts": AccountStore(paths).list()}

    @app.get('/api/account-pools')
    async def account_pools():
        from .core.account_pool import AccountPoolStore
        return {'pools': await asyncio.to_thread(AccountPoolStore(paths, store).list)}

    @app.put('/api/account-pools/{name}')
    async def save_account_pool(name: str, body: PoolDefinition):
        from .core.account_pool import AccountPoolStore
        try:
            return await asyncio.to_thread(AccountPoolStore(paths, store).save, name,
                body.platform, body.members, expected_revision=body.expected_revision)
        except NeedsLogin:
            raise HTTPException(422, '请先完成所选账号的登录。') from None
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, '选择已保存的账号和已登记的代理；每个账号只能绑定一条代理。') from None

    @app.patch('/api/account-pools/{name}/members/{account}')
    async def set_pool_member(name: str, account: str, body: PoolMemberState):
        from .core.account_pool import AccountPoolStore
        try:
            return await asyncio.to_thread(AccountPoolStore(paths, store).set_member_state,
                name, account, body.state, expected_revision=body.expected_revision)
        except NeedsLogin:
            raise HTTPException(422, '请先重新登录该账号，再启用池成员。') from None
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, '选择池中的账号并设置 enabled 或 paused。') from None

    @app.get('/api/browser-profiles')
    async def browser_profiles():
        from .core.browser_profiles import BrowserProfileStore
        return {'profiles':await asyncio.to_thread(BrowserProfileStore(paths).list_profiles)}

    @app.delete('/api/browser-profiles/{name}')
    async def delete_browser_profile(name:str):
        from .core.browser_profiles import BrowserProfileStore,ProfileBusy
        try:
            return {'deleted':await asyncio.to_thread(BrowserProfileStore(paths).delete,name)}
        except ProfileBusy:raise HTTPException(409,'Browser profile is in use')
        except (ValueError,OSError):raise HTTPException(422,'Invalid or unavailable project browser profile')

    @app.post('/api/browser-profiles/{name}/shrink')
    async def shrink_browser_profile(name:str,level:str='light',apply:bool=False):
        from .core.browser_profiles import BrowserProfileStore,ProfileBusy
        try:
            return await asyncio.to_thread(BrowserProfileStore(paths).shrink,name,level,dry_run=not apply)
        except ProfileBusy:raise HTTPException(409,'Browser profile is in use')
        except (ValueError,OSError):raise HTTPException(422,'Invalid or unavailable project browser profile')

    @app.post("/api/accounts", status_code=201)
    async def add_account(body: NewAccount):
        try:
            return AccountStore(paths).create(body.platform, body.account, domains=body.domains)
        except ValueError:
            raise HTTPException(422, "Choose a supported platform and an account name using letters, digits, dots, dashes or underscores")

    @app.delete("/api/accounts/{platform}/{account}")
    async def revoke_account(platform: str, account: str):
        for job_id in active:
            active_job = store.job(job_id)
            params, kind = active_job["params"], active_job["kind"]
            selected_platform = params.get("platform", "bili" if kind == "platform.collect" else "web")
            selected_account = params.get("account", "main" if kind in {"account.login","account.check"} else None)
            if selected_platform == platform and selected_account == account:
                raise HTTPException(409, "Finish or cancel the selected account's active task before revoking its state")
        try:
            result = AccountStore(paths).revoke(platform, account)
            if hasattr(app.state, 'cookiebridge'):
                app.state.cookiebridge.revoke_account(platform, account)
            return result
        except ValueError:
            raise HTTPException(422, "Invalid account name")

    @app.get("/api/proxies")
    async def proxies():
        return {"proxies": ProxyStore(paths).list()}

    @app.get("/api/export-targets")
    async def export_targets():
        from .exports.remote import RemoteTargetStore
        return {"targets": RemoteTargetStore(paths).list()}

    @app.post("/api/export-targets", status_code=201)
    async def save_export_target(request: Request):
        from .exports.remote import RemoteTargetStore
        # Credentials bypass task history and Pydantic error echoes.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise HTTPException(413, "Database configuration exceeds the size limit")
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) - {'name', 'backend', 'dsn', 'database', 'project_upstream'}:
                raise ValueError()
            if any(store.job(job_id)['kind'] == 'export.remote' and
                   store.job(job_id)['params'].get('target_name') == value.get('name') for job_id in active):
                raise HTTPException(409, "Finish or cancel this target's active export first")
            return RemoteTargetStore(paths).save(value['name'], value['backend'], value['dsn'],
                database=value.get('database'), project_upstream=value.get('project_upstream', True))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Provide a valid named database target; credentials were not returned") from None

    @app.delete("/api/export-targets/{name}")
    async def delete_export_target(name: str):
        from .exports.remote import RemoteTargetStore
        if any(store.job(job_id)['kind'] == 'export.remote' and
               store.job(job_id)['params'].get('target_name') == name for job_id in active):
            raise HTTPException(409, "Finish or cancel this target's active export first")
        try:
            RemoteTargetStore(paths).delete(name)
            return {'name': name, 'deleted': True}
        except ValueError:
            raise HTTPException(422, "Invalid database target name") from None

    @app.post("/api/proxies", status_code=201)
    async def add_proxy(body: NewProxy):
        try:
            return ProxyStore(paths).save(body.name, body.url)
        except ValueError:
            raise HTTPException(422, "Expected a named HTTP/HTTPS/SOCKS5 proxy endpoint")

    def launch(job_id):
        task = asyncio.create_task(runner.run(job_id))
        active[job_id] = task
        def finished(done):
            active.pop(job_id, None)
            if not done.cancelled():
                done.exception()  # Consume worker failures; persisted state remains authoritative.
        task.add_done_callback(finished)

    @app.post("/api/jobs", status_code=202)
    async def submit(body: NewJob):
        if body.kind not in runner.handlers:
            raise HTTPException(422, "This capability is not implemented in the current build")
        if len(active) >= 2:
            raise HTTPException(429, "Two tasks are already running; wait or pause one")
        if body.kind == 'export.remote':
            from .exports.remote_jobs import validate_params
            try:
                validate_params(body.params)
            except (ValueError, TypeError):
                raise HTTPException(422, "Use a named database target; connection credentials must not be job parameters") from None
        if body.kind == "web.collect":
            urls = body.params.get("urls", [])
            local_files = body.params.get("local_files", [])
            raw_html = body.params.get("raw_html")
            if not isinstance(urls, list) or not isinstance(local_files, list) or len(urls) + len(local_files) > 50:
                raise HTTPException(422, "Provide at most 50 explicit sources")
            if raw_html is not None and (not isinstance(raw_html, str) or not raw_html.strip() or len(raw_html.encode("utf-8")) > 5_000_000):
                raise HTTPException(422, "raw_html must contain at most 5000000 UTF-8 bytes")
            if not urls and not local_files and raw_html is None:
                raise HTTPException(422, "Provide urls, local_files or raw_html")
            for value in local_files:
                if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                    raise HTTPException(422, "local_files must identify explicit existing absolute file paths")
            for url in urls:
                try:
                    parsed = urlparse(url) if isinstance(url, str) else None
                    valid = parsed is not None and parsed.scheme in {"http", "https"} and parsed.hostname
                    if parsed and parsed.scheme == "raw":
                        valid = bool(parsed.path or parsed.netloc) and len(url.encode("utf-8")) <= 5_000_000
                    elif parsed and parsed.scheme == "file":
                        from .web.inputs import local_path
                        valid = local_path(url).is_file()
                except (ValueError, OSError):
                    valid = False
                if not valid:
                    raise HTTPException(422, "Each URL must be an HTTP(S), explicit local file or raw source")
            if type(body.params.get("max_pages", 8)) is not int or not 1 <= body.params.get("max_pages", 8) <= 50:
                raise HTTPException(422, "max_pages must be between 1 and 50")
        job = store.create_job(body.kind, body.params)
        launch(job["id"])
        return job

    @app.get("/api/jobs/{job_id}")
    async def inspect_job(job_id: str):
        job = store.job(job_id)
        return {"job": job, "counts": store.counts(job_id), "resume_limits": platform_resume_limits(job),
                "records": store.observations(job_id), "events": store.events(job_id),
                "waiting_for_login": job_id in getattr(getattr(runner, "login_manager", None), "waiting", {})}

    @app.post("/api/jobs/{job_id}/{action}")
    async def control(job_id: str, action: str, body: dict | None = None):
        if body and (action != "resume" or set(body) - {"max_items"}):
            raise HTTPException(422, "Only resume accepts the max_items option")
        if body is not None and "max_items" in body and (type(body["max_items"]) is not int or not 1 <= body["max_items"] <= 1000):
            raise HTTPException(422, "max_items must be an integer from 1 to 1000")
        job = store.job(job_id)
        if action == "confirm-login":
            manager = getattr(runner, "login_manager", None)
            if manager is None or job_id not in manager.waiting:
                raise HTTPException(409, "This task is not waiting for a login confirmation")
            manager.confirm(job_id)
        elif action == "resume":
            if job_id in active:
                raise HTTPException(409, "Task is already active")
            if len(active) >= 2:
                raise HTTPException(429, "Worker capacity reached")
            if job["kind"] not in runner.handlers:
                raise HTTPException(422, "Task handler unavailable")
            try:
                store.resume_job(job_id, max_items=(body or {}).get("max_items"), expected_status=job["status"])
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            launch(job_id)
        elif action in {"pause", "cancel"}:
            if job["status"] == "running":
                store.request_action(job_id, action)
            elif action == "cancel":
                store.transition(job_id, "cancelled")
            else:
                raise HTTPException(409, "Only a running task can pause")
        else:
            raise HTTPException(404, "Unknown action")
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/export/json")
    async def export(job_id: str):
        job = store.job(job_id)
        rows, offset = [], 0
        while True:
            batch = store.observations(job_id, limit=1000, offset=offset)
            rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += len(batch)
        return JSONResponse({"job": job, "records": rows}, headers={
            "Content-Disposition": f'attachment; filename="research-{job_id}.json"'})

    @app.get("/api/jobs/{job_id}/files/{record_key}/{file_index}")
    async def export_file(job_id: str, record_key: str, file_index: int):
        store.job(job_id)
        with store.connection() as db:
            row = db.execute("""SELECT v.payload_json FROM observations o JOIN record_versions v
                ON v.record_key=o.record_key AND v.content_hash=o.content_hash
                WHERE o.job_id=? AND o.record_key=? ORDER BY o.observed_at DESC LIMIT 1""", (job_id, record_key)).fetchone()
        if row is None:
            raise HTTPException(404, "File record not found")
        payload = json.loads(row[0])
        files = payload.get("files", [])
        if not files and isinstance(payload.get("artifacts"), dict):
            files = [{"path": path} for _, path in sorted(payload["artifacts"].items())]
        if file_index < 0 or file_index >= len(files):
            raise HTTPException(404, "File not found")
        file = Path(files[file_index].get("path", "")).resolve()
        if not file.is_relative_to(paths.run(job_id).resolve()) or not file.is_file():
            raise HTTPException(404, "File is unavailable or outside this task")
        return FileResponse(file, filename=file.name, media_type="application/octet-stream")

    from .adapters.cookiebridge import register_routes
    register_routes(app, paths, store, runner)
    return app
