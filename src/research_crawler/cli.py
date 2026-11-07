from __future__ import annotations

import argparse
import asyncio
import json

from . import __version__
from .core.paths import AppPaths
from .core.store import StateConflict, Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="research-crawler", description="Unified local research runtime",
        epilog="Includes software developed by UncleCode as part of Crawl4AI (https://github.com/unclecode/crawl4ai). MediaCrawler source remains under NON-COMMERCIAL LEARNING LICENSE 1.1.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--home", help="Explicit application state root (F: on this workstation)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show runtime capabilities, model health and recent job states")
    commands.add_parser("doctor", help="Read-only runtime, dependency, asset and attribution diagnosis")
    commands.add_parser("crawlers", help="List source-integrated dedicated extraction profiles")
    inspect = commands.add_parser("job", help="Inspect a persisted job")
    inspect.add_argument("id")
    inspect.add_argument("--records", action="store_true", help="Include the first 100 evidence records")
    serve = commands.add_parser("serve", help="Open the unified loopback-only local application")
    serve.add_argument("--port", type=int, default=8946)
    collect = commands.add_parser("collect", help="Collect pages through the unified task runtime")
    collect.add_argument("urls", nargs="+")
    collect.add_argument("--max-pages", type=int, default=8)
    run = commands.add_parser("run", help="Run one registered capability through the same shared runtime")
    run.add_argument("kind")
    run.add_argument("--params-file", required=True, help="UTF-8 JSON object with job parameters")
    resume = commands.add_parser("resume", help="Resume an existing task without changing its sources or identity")
    resume.add_argument("id")
    resume.add_argument("--max-items", type=int, help="Explicitly increase a stopped platform task's cumulative record limit (1..1000)")
    targets = commands.add_parser('export-target', help='Manage private named database export targets')
    targets.add_argument('action', choices=['list', 'save', 'delete'])
    targets.add_argument('--name')
    targets.add_argument('--backend', choices=['mysql', 'postgresql', 'mongodb'])
    targets.add_argument('--database', help='Required MongoDB database name')
    targets.add_argument('--dsn-stdin', action='store_true', help='Read connection URI from standard input instead of the hidden prompt')
    targets.add_argument('--no-upstream', action='store_true', help='Export shared evidence only, without original platform table projections')
    profiles=commands.add_parser('browser-profile',help='Manage project-owned persistent browser profiles')
    profiles.add_argument('action',choices=['list','delete','shrink','expire'])
    profiles.add_argument('--name')
    profiles.add_argument('--level',choices=['none','light','medium','aggressive','minimal'],default='light')
    profiles.add_argument('--apply',action='store_true',help='Apply shrink changes (default is preview)')
    profiles.add_argument('--max-idle-seconds',type=float)
    args = parser.parse_args()
    paths = AppPaths.discover(args.home)
    if args.command == 'crawlers':
        from .web.crawlers import list_crawlers
        print(json.dumps({'crawlers':list_crawlers()},ensure_ascii=False,indent=2))
        return
    if args.command == 'doctor':
        from .core.doctor import diagnose
        print(json.dumps(diagnose(paths.root), ensure_ascii=False, indent=2))
        return
    paths.initialize()
    store = Store(paths.database)
    if args.command=='browser-profile':
        from .core.browser_profiles import BrowserProfileStore,ProfileBusy
        profiles=BrowserProfileStore(paths)
        try:
            if args.action=='list':output={'profiles':profiles.list_profiles()}
            elif args.action=='expire':
                if args.max_idle_seconds is None:parser.error('--max-idle-seconds is required')
                output=profiles.expire(args.max_idle_seconds)
            else:
                if not args.name:parser.error('--name is required')
                output=({'deleted':profiles.delete(args.name)} if args.action=='delete' else
                        profiles.shrink(args.name,args.level,dry_run=not args.apply))
            print(json.dumps(output,ensure_ascii=False,indent=2))
        except (ValueError,OSError,ProfileBusy):
            parser.error('Profile unavailable, invalid, or in use; no browser credentials returned')
        return
    if args.command == 'export-target':
        from .exports.remote import RemoteTargetStore
        targets = RemoteTargetStore(paths)
        try:
            if args.action == 'list':
                output = {'targets': targets.list()}
            else:
                if not args.name:
                    parser.error('--name is required')
                if args.action == 'delete':
                    targets.delete(args.name)
                    output = {'name': args.name, 'deleted': True}
                else:
                    if not args.backend:
                        parser.error('--backend is required')
                    import getpass
                    import sys
                    dsn = sys.stdin.readline(8193).strip() if args.dsn_stdin else getpass.getpass('Database connection URI: ')
                    output = targets.save(args.name, args.backend, dsn, database=args.database,
                                          project_upstream=not args.no_upstream)
            print(json.dumps(output, ensure_ascii=False, indent=2))
        except (ValueError, TypeError, OSError):
            parser.error('Cannot configure the named database target; credentials were not returned')
        return
    if args.command == "serve":
        import uvicorn
        from .service import create_app
        uvicorn.run(create_app(paths.root), host="127.0.0.1", port=args.port, access_log=False)
        return
    if args.command == "resume":
        from .service import build_runner
        runner, _ = build_runner(store, paths)
        try:
            job = store.job(args.id)
            if job["kind"] not in runner.handlers:
                parser.error("Task handler unavailable")
            store.resume_job(args.id, max_items=args.max_items, expected_status=job["status"])
            result = asyncio.run(runner.run(args.id))
        except (ValueError, KeyError, StateConflict) as error:
            parser.error(str(error) if not isinstance(error, KeyError) else "Task not found")
        print(json.dumps({"job": result, "counts": store.counts(args.id)}, ensure_ascii=False, indent=2))
        if result["status"] not in {"complete", "partial"}:
            raise SystemExit(1)
        return
    if args.command in {"collect", "run"}:
        from .service import build_runner
        runner, _ = build_runner(store, paths)
        if args.command == "collect":
            kind, params = "web.collect", {"urls": args.urls, "max_pages": args.max_pages}
        else:
            from pathlib import Path
            kind = args.kind
            try:
                params = json.loads(Path(args.params_file).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                parser.error(f"Cannot read parameter file: {type(error).__name__}")
            if not isinstance(params, dict):
                parser.error("Parameter file must contain a JSON object")
        if kind not in runner.handlers:
            parser.error(f"Capability unavailable: {kind}. Available: {', '.join(runner.handlers)}")
        if kind == 'export.remote':
            from .exports.remote_jobs import validate_params
            try:
                validate_params(params)
            except (ValueError, TypeError):
                parser.error('Use target_name only; database credentials must not be job parameters')
        job = store.create_job(kind, params)
        result = asyncio.run(runner.run(job["id"]))
        print(json.dumps({"job": result, "counts": store.counts(job["id"])}, ensure_ascii=False, indent=2))
        if result["status"] not in {"complete", "partial"}:
            raise SystemExit(1)
        return
    if args.command == "status":
        from .service import build_runner, codex_model_health
        runner, modules = build_runner(store, paths)
        from .intelligence.local_model import LocalModelClient

        async def model_health():
            async with LocalModelClient(paths.root, timeout=3) as model:
                local, codex = await asyncio.gather(model.health(),codex_model_health(paths.root))
                return local, codex

        local_health, codex_health = asyncio.run(model_health())
        output = {"version": __version__, "root": str(paths.root), "database": str(paths.database),
                  "jobs": [{key: job[key] for key in ("id", "kind", "status", "updated_at")} for job in store.jobs()[:10]],
                  "handlers": list(runner.handlers), "modules": modules, "model": local_health, "codex_model":codex_health,
                  "stage": "partial-source-migration"}
    else:
        output = {"job": store.job(args.id), "counts": store.counts(args.id), "events": store.events(args.id)}
        if args.records:
            output["records"] = store.observations(args.id, limit=100)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
