"""Read-only local installation diagnosis and metadata-derived CycloneDX inventory.

Never imports installed libraries, reads account/token stores, starts processes or
contacts a service. Recorded model hashes are not presented as current verification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import quote

from .paths import AppPaths

HASH_LIMIT = 32 * 1024 * 1024
ENV_PATHS = ("TEMP", "TMP", "PIP_CACHE_DIR", "UV_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH",
             "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "NLTK_DATA")
ASSET_MANIFESTS = ("runtime/media-model-manifest.json", "runtime/semantic-model-manifest.json",
                   "runtime/semantic-topic-manifest.json", "runtime/vision-model-manifest.json",
                   "runtime/creation-manifest.json", "artifacts/model-download-manifest.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _inside(root: Path, value: str | Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path_outside_project")
    return path


def _json(root, name):
    path = _inside(root, name)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("manifest_too_large")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _hash(path):
    if path.stat().st_size > HASH_LIMIT:
        raise ValueError("hash_size_limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_hash(root, name, expected):
    result = {"path": str(name).replace("\\", "/"), "expected_sha256": expected}
    try:
        path = _inside(root, name)
        actual = _hash(path)
        result.update(status="pass" if actual == expected else "error", actual_sha256=actual)
    except (OSError, ValueError) as exc:
        result.update(status="error", reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    return result


def source_checks(root):
    checks = []
    for label in ("crawl4ai", "mediacrawler"):
        manifest_name = f"source-freeze-{label}.json"
        try:
            source = _json(root, manifest_name)
            checks.append(_check_hash(root, source["archive_path"], source["archive_sha256"]))
            if label == "crawl4ai":
                license_info = source["license"]
                checks.append(_check_hash(root, license_info["path"], license_info["sha256"]))
            else:
                license_info = next(item for item in source["files"] if item["path"] == "LICENSE")
                checks.append(_check_hash(root, "licenses/mediacrawler/LICENSE", license_info["sha256"]))
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            checks.append({"path": manifest_name, "status": "error", "reason": type(exc).__name__})
    # Verify every migrated target of the primary 32-module web inventory.
    name = "src/research_crawler/web/source-manifest.json"
    try:
        for entry in _json(root, name)["files"]:
            checks.append(_check_hash(root, "src/research_crawler/web/" + entry["target"], entry["target_sha256"]))
    except (OSError, ValueError, KeyError) as exc:
        checks.append({"path": name, "status": "error", "reason": type(exc).__name__})
    return checks


def _environments(root):
    # Explicit product-owned worker environments, never arbitrary recursive scans.
    for name in ("venv", "media-model-venv", "semantic-model-venv"):
        try:
            base = _inside(root, "runtime/" + name)
        except ValueError:
            yield name, []
            continue
        candidates = [base / "Lib/site-packages", *base.glob("lib/python*/site-packages")]
        present = [p for p in candidates if p.is_dir() and p.resolve().is_relative_to(root)]
        yield name, present


def _canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_inventory(root):
    inventory = []
    for environment, paths in _environments(root):
        for dist in metadata.distributions(path=[str(p) for p in paths]):
            name = dist.metadata.get("Name")
            if not name:
                continue
            text = dist.read_text("METADATA") or ""
            expression = dist.metadata.get("License-Expression")
            raw_license = dist.metadata.get("License")
            # Large historical license texts stay in the package; do not flood SBOM.
            license_name = raw_license.strip() if raw_license and len(raw_license) <= 256 else None
            inventory.append({"environment": environment, "name": name, "version": dist.version,
                              "license_expression": expression, "license_name": license_name,
                              "license_status": "declared" if expression or license_name else "unknown",
                              "metadata_sha256": hashlib.sha256(text.encode()).hexdigest(),
                              "requires": dist.requires or []})
    return sorted(inventory, key=lambda item: (item["environment"], _canon(item["name"])))


def dependency_checks(root, packages):
    from packaging.requirements import Requirement, InvalidRequirement
    from packaging.version import Version, InvalidVersion
    environments = {}
    for item in packages:
        environments.setdefault(item["environment"], {})[_canon(item["name"])] = item
    checks = []
    for item in packages:
        for spec in item["requires"]:
            try:
                requirement = Requirement(spec)
                if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                    continue
                installed = environments[item["environment"]].get(_canon(requirement.name))
                compatible = installed is not None and (not requirement.specifier or Version(installed["version"]) in requirement.specifier)
                if not compatible:
                    checks.append({"status": "error", "environment": item["environment"],
                                   "package": item["name"], "dependency": requirement.name,
                                   "reason": "missing_dependency" if installed is None else "version_mismatch"})
            except (InvalidRequirement, InvalidVersion):
                checks.append({"status": "warning", "package": item["name"], "reason": "unparseable_dependency_metadata"})
    # A consistent empty environment must not pass the application contract.
    try:
        project = tomllib.loads(_inside(root, "pyproject.toml").read_text(encoding="utf-8"))["project"]
        main = environments.get("venv", {})
        for spec in project.get("dependencies", []):
            req = Requirement(spec)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            installed = main.get(_canon(req.name))
            if installed is None or (req.specifier and Version(installed["version"]) not in req.specifier):
                checks.append({"status": "error", "package": req.name, "reason": "application_requirement_unsatisfied"})
    except (OSError, ValueError, KeyError) as exc:
        checks.append({"status": "error", "reason": "project_contract_unreadable", "type": type(exc).__name__})
    return checks


def asset_inventory(root):
    assets = []
    optional = ('artifacts/model-qwen8b-download-manifest.json', 'artifacts/model-instruct4b-download-manifest.json',
                'runtime/creation-instruction-manifest.json')
    manifests = ASSET_MANIFESTS + tuple(name for name in optional if (root / name).exists())
    for name in manifests:
        try:
            document = _json(root, name)
            # Only allowlisted provenance facts; credentials, logs and arbitrary text are excluded.
            files = []
            def visit(value):
                if isinstance(value, dict):
                    if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
                        item = {"path": value["path"], "recorded_sha256": value["sha256"], "hash_verified_now": False}
                        if name == 'runtime/creation-instruction-manifest.json':
                            for role in ('model', 'encoders'):
                                if value is document.get(role):
                                    item['role'] = role
                                    for key in ('license', 'source_path', 'source_sha256', 'source_revision'):
                                        if isinstance(value.get(key), str):
                                            item[key] = value[key]
                        try:
                            path = _inside(root, value["path"])
                            item.update(present=path.is_file(), actual_bytes=path.stat().st_size)
                            if isinstance(value.get("bytes"), int):
                                item["recorded_bytes"] = value["bytes"]
                                item["size_matches"] = value["bytes"] == item["actual_bytes"]
                        except (OSError, ValueError):
                            item.update(present=False, reason="missing_or_outside_project")
                        files.append(item)
                    for child in value.values():visit(child)
                elif isinstance(value, list):
                    for child in value:visit(child)
            visit(document)
            assets.append({"manifest": name, "manifest_sha256": _hash(_inside(root, name)), "files": files})
        except (OSError, ValueError):
            assets.append({"manifest": name, "status": "unavailable"})
    browsers = []
    try:
        browser_root = _inside(root, "runtime/browsers")
    except ValueError:
        return {"manifests": assets, "browsers": [], "browser_path_error": "path_outside_project"}
    for name in ("chromium", "firefox", "webkit"):
        for directory in sorted(browser_root.glob(name + "-*")):
            if directory.is_dir() and directory.resolve().is_relative_to(root):
                executables = [p for p in directory.rglob("*.exe") if p.name.lower() in {"chrome.exe", "firefox.exe", "playwright.exe", "minibrowser.exe"}]
                browsers.append({"name": directory.name, "executable_present": bool(executables), "launch_verified_now": False})
    return {"manifests": assets, "browsers": browsers}


def diagnose(root=None):
    root = AppPaths.discover(root).root
    paths = []
    for name in ("runtime", "cache", "temp", "models", "state", "runs", "output"):
        try:
            path = _inside(root, name)
            paths.append({"path": name, "status": "pass" if path.is_dir() else "warning", "reason": "contained" if path.is_dir() else "missing"})
        except ValueError:
            paths.append({"path": name, "status": "error", "reason": "path_outside_project"})
    for key in ENV_PATHS:
        value = os.environ.get(key)
        if value:
            paths.append({"environment_key": key, "status": "pass" if Path(value).resolve().is_relative_to(root) else "warning", "reason": "contained" if Path(value).resolve().is_relative_to(root) else "outside_project"})
        else:
            paths.append({"environment_key": key, "status": "warning", "reason": "unset"})
    sources = source_checks(root)
    packages = installed_inventory(root)
    dependencies = dependency_checks(root, packages)
    assets = asset_inventory(root)
    interpreter = {"version": sys.version.split()[0], "supported": sys.version_info >= (3, 12),
                   "project_owned": Path(sys.prefix).resolve().is_relative_to(root),
                   "requested_drive": "F:", "root_on_requested_drive": root.drive.upper() == "F:" if os.name == "nt" else None}
    errors = sum(item["status"] == "error" for item in paths + sources + dependencies)
    if not interpreter["supported"]:errors += 1
    if not interpreter["project_owned"]:errors += 1
    if interpreter['root_on_requested_drive'] is False:errors += 1
    warnings = sum(item["status"] == "warning" for item in paths + dependencies)
    warnings += sum(item.get('status') == 'unavailable' for item in assets['manifests'])
    warnings += sum(not file.get('present') or file.get('size_matches') is False for item in assets['manifests'] for file in item.get('files', []))
    warnings += sum(not item['executable_present'] for item in assets['browsers'])
    return {"schema_version": 1, "recorded_at": _now(), "status": "error" if errors else "warning" if warnings else "ok", "warnings": warnings,
            "errors": errors, "interpreter": interpreter, "paths": paths, "source_integrity": sources,
            "dependency_issues": dependencies, "package_counts": {env: sum(p["environment"] == env for p in packages) for env, _ in _environments(root)},
            "assets": assets,
            "limits": ["No service, browser launch, model inference or authenticated platform probe", "Model hashes are recorded provenance, not freshly verified weights", "Source checks cover frozen archives/licenses and primary web migration targets, not all later manifests", "Environment and dependency markers use this host; Linux compatibility is not proved", "Dependency edges evaluate default markers; optional-extra closure requires the package manager compatibility check", "Unknown licenses require review; inventory is not legal clearance"]}


def build_sbom(root=None):
    root = AppPaths.discover(root).root
    packages = installed_inventory(root)
    components, dependencies = [], []
    refs = {(p["environment"], _canon(p["name"])): f"python:{p['environment']}:{_canon(p['name'])}@{p['version']}" for p in packages}
    from packaging.requirements import Requirement, InvalidRequirement
    for p in packages:
        ref = refs[p["environment"], _canon(p["name"])]
        component = {"type": "library", "bom-ref": ref, "name": p["name"], "version": p["version"],
                     "purl": f"pkg:pypi/{quote(_canon(p['name']))}@{quote(p['version'])}",
                     "properties": [{"name": "research:environment", "value": p["environment"]},
                                    {"name": "research:license-status", "value": p["license_status"]},
                                    {"name": "research:metadata-sha256", "value": p["metadata_sha256"]}]}
        if p["license_expression"]:component["licenses"] = [{"expression": p["license_expression"]}]
        elif p["license_name"]:component["licenses"] = [{"license": {"name": p["license_name"]}}]
        components.append(component)
        edges = set()
        for spec in p["requires"]:
            try:
                req = Requirement(spec)
                if req.marker and not req.marker.evaluate({"extra": ""}):continue
                target = refs.get((p["environment"], _canon(req.name)))
                if target:edges.add(target)
            except InvalidRequirement:pass
        dependencies.append({"ref": ref, "dependsOn": sorted(edges)})
    for label, license_name in (("crawl4ai", "Apache-2.0 with retained attribution"), ("mediacrawler", "NON-COMMERCIAL LEARNING LICENSE 1.1")):
        data = _json(root, f"source-freeze-{label}.json")
        components.append({"type": "library", "bom-ref": "source:" + label, "name": label, "version": data["commit"],
                           "licenses": [{"license": {"name": license_name}}],
                           "externalReferences": [{"type": "vcs", "url": data["repository"]}],
                           "properties": [{"name": "research:frozen-archive-sha256", "value": data["archive_sha256"]}]})
    assets = asset_inventory(root)
    for item in assets["manifests"]:
        if "manifest_sha256" in item:
            components.append({"type": "data", "bom-ref": "manifest:" + item["manifest"], "name": item["manifest"],
                               "hashes": [{"alg": "SHA-256", "content": item["manifest_sha256"]}],
                               "properties": [{"name": "research:scope", "value": "Asset provenance manifest; large asset hashes not reverified"}]})
            for asset in item.get('files', []):
                if asset.get('role') not in ('model', 'encoders'):
                    continue
                component = {'type': 'data', 'bom-ref': 'asset:' + asset['path'].replace('\\', '/'),
                             'name': asset['path'].replace('\\', '/'),
                             'properties': [{'name': 'research:manifest', 'value': item['manifest']},
                                            {'name': 'research:asset-role', 'value': asset['role']},
                                            {'name': 'research:recorded-sha256', 'value': asset['recorded_sha256']},
                                            {'name': 'research:hash-verified-now', 'value': 'false'}]}
                if asset.get('license'):
                    component['licenses'] = [{'license': {'name': asset['license']}}]
                for key in ('source_path', 'source_sha256', 'source_revision'):
                    if key in asset:
                        component['properties'].append({'name': 'research:' + key.replace('_', '-'), 'value': asset[key]})
                components.append(component)
    for item in assets["browsers"]:
        components.append({"type": "application", "bom-ref": "browser:" + item["name"], "name": item["name"],
                           "properties": [{"name": "research:license-status", "value": "unknown; bundled browser dependency inventory not expanded"}]})
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "metadata": {"timestamp": _now(), "component": {"type": "application", "bom-ref": "research-crawler", "name": "research-crawler", "version": "0.1.0.dev0"},
                         "properties": [{"name": "research:scope", "value": "Installed Python distributions in three owned environments, two frozen sources, browser revisions and asset manifest references; no bundled native dependency expansion or license clearance"}]},
            "components": components, "dependencies": [{"ref": "research-crawler", "dependsOn": [c["bom-ref"] for c in components]}] + dependencies}
