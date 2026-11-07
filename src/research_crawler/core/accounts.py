"""Selected-site session storage. Credentials are never part of job records."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import os
import time
import uuid
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .jobs import NeedsLogin
from .paths import AppPaths, safe_name
from .records import utcnow


PLATFORM_SITES = {
    "xhs": ("https://www.xiaohongshu.com", ("xiaohongshu.com", "rednote.com")),
    "dy": ("https://www.douyin.com", ("douyin.com",)),
    "ks": ("https://www.kuaishou.com", ("kuaishou.com",)),
    "bili": ("https://www.bilibili.com", ("bilibili.com",)),
    "wb": ("https://weibo.com", ("weibo.com", "weibo.cn", "sina.com.cn")),
    "tieba": ("https://tieba.baidu.com", ("baidu.com",)),
    "zhihu": ("https://www.zhihu.com", ("zhihu.com",)),
    "reddit": ("https://www.reddit.com", ("reddit.com",)),
}


def matches_domain(host: str, domains: tuple[str, ...] | list[str]) -> bool:
    host = host.lower().lstrip(".").rstrip(".")
    return any(host == item or host.endswith("." + item) for item in domains)


def scoped_state(state: dict, domains) -> dict:
    now = time.time()
    cookies = []
    for cookie in state.get("cookies", []):
        expires = cookie.get("expires", -1)
        if (matches_domain(cookie.get("domain", ""), domains)
                and (expires <= 0 or expires > now)):
            cookies.append({key: cookie[key] for key in (
                "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey")
                if key in cookie})
    origins = []
    for origin in state.get("origins", []):
        try:
            parsed = urlsplit(origin.get("origin", ""))
            if parsed.scheme in {"https", "http"} and matches_domain(parsed.hostname or "", domains):
                origins.append({"origin": origin["origin"], "localStorage": origin.get("localStorage", [])})
        except ValueError:
            continue
    return {"cookies": cookies, "origins": origins}


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    encoded = b"DPAPI1\n" + _dpapi(payload, decrypt=False) if os.name == "nt" else b"PRIVATE1\n" + payload
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    temporary.replace(path)


def read_private(path: Path) -> dict:
    data = path.read_bytes()
    if data.startswith(b"DPAPI1\n"):
        if os.name != "nt":
            raise RuntimeError("Windows account storage must be opened by its owning Windows user")
        data = _dpapi(data[7:], decrypt=True)
    elif data.startswith(b"PRIVATE1\n"):
        data = data[9:]
    else:
        raise ValueError("Unsupported private storage format")
    return json.loads(data)


class AccountStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    @staticmethod
    @contextmanager
    def _locked(folder):
        """Short cross-process metadata updates; never hold this over a network call."""
        folder.mkdir(parents=True,exist_ok=True)
        with (folder/'.account.lock').open('a+b') as handle:
            if handle.seek(0,2)==0:
                handle.write(b'0');handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            try:yield
            finally:
                handle.seek(0)
                if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def domains(self, platform: str, account: str | None = None) -> tuple[str, ...]:
        if platform in PLATFORM_SITES:
            return PLATFORM_SITES[platform][1]
        if platform == "web" and account:
            return tuple(self.info(platform, account)["domains"])
        if platform == "web":
            return ()
        raise ValueError("Unknown platform")

    def info(self, platform: str, account: str) -> dict:
        path = self.paths.profile(platform, account) / "account.json"
        if not path.is_file():
            raise KeyError(account)
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, platform: str, account: str, *, domains: list[str] | None = None) -> dict:
        folder = self.paths.profile(platform, account)
        if platform == "web":
            if (folder/'account.json').exists():return self.info(platform,account)
            scopes = tuple(str(value).strip().lower().rstrip(".") for value in (domains or []))
            if not scopes or any(not value or "/" in value or ":" in value or value.startswith(".") or " " in value for value in scopes):
                raise ValueError("Web account needs explicit host domains")
        else:
            scopes = self.domains(platform)
        with self._locked(folder):
            if (folder/'account.json').exists():return self.info(platform,account)
            info = {"platform": platform, "account": account, "domains": scopes, "status": "not_logged_in",
                    "saved_at": None, "cookie_count": 0, "session_revision":uuid.uuid4().hex,
                    "protection": "windows_dpapi" if os.name == "nt" else "file_permissions"}
            self._write_info(folder, info)
            return info

    @staticmethod
    def _write_info(folder: Path, info: dict) -> None:
        temporary = folder / ("account."+uuid.uuid4().hex+".tmp")
        temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "account.json")

    def save(self, platform: str, account: str, state: dict, *, domains=None) -> dict:
        self.create(platform, account, domains=domains)
        filtered = scoped_state(state, self.domains(platform, account))
        if not filtered["cookies"] and not filtered["origins"]:
            raise NeedsLogin(platform, account, "No selected-site session state was found")
        folder = self.paths.profile(platform, account)
        with self._locked(folder):
            write_private(folder / "storage_state.private", filtered)
            # A successfully protected save replaces a legacy unprotected copy.
            legacy = folder / "storage_state.json"
            if legacy.is_file():
                legacy.unlink()
            info = self.info(platform, account)
            info.update(status="saved_unverified", saved_at=utcnow(), cookie_count=len(filtered["cookies"]),
                        session_revision=uuid.uuid4().hex)
            info.pop('validation',None)
            self._write_info(folder, info)
        return info

    def validation_revision(self,platform,account):
        info=self.info(platform,account)
        return info.get('session_revision',info.get('saved_at'))

    def record_validation(self,platform,account,result,*,expected_revision):
        """Publish a bounded receipt only if the checked saved session still exists."""
        state=result.get('state')
        if state not in {'valid','auth_required','indeterminate'}:
            raise ValueError('Unsupported session validation state')
        folder=self.paths.profile(platform,account)
        with self._locked(folder):
            info=self.info(platform,account)
            if info.get('session_revision',info.get('saved_at'))!=expected_revision or info['status']=='revoked':
                return None
            receipt={'state':state,'checked_at':utcnow(),'reason_code':str(result.get('reason_code','unknown'))[:100],
                     'method':str(result.get('method','unknown'))[:120]}
            if type(result.get('http_status')) is int:receipt['http_status']=result['http_status']
            if type(result.get('network_used')) is bool:receipt['network_used']=result['network_used']
            receipt['validity_scope']='response_at_check_time_only' if state=='valid' else 'not_verified'
            info.update(status='verified' if state=='valid' else 'auth_required' if state=='auth_required' else 'validation_unknown',
                        validation=receipt)
            self._write_info(folder,info)
            return info

    def state(self, platform: str, account: str | None, *, required: bool = False) -> dict:
        if account is None:
            if required:
                raise NeedsLogin(platform, "selected_account")
            return {"cookies": [], "origins": []}
        folder = self.paths.profile(platform, account)
        path = folder / "storage_state.private"
        if path.is_file():
            state = read_private(path)
        elif (folder / "storage_state.json").is_file():
            state = json.loads((folder / "storage_state.json").read_text(encoding="utf-8"))
        else:
            raise NeedsLogin(platform, account)
        filtered = scoped_state(state, self.domains(platform, account))
        if not filtered["cookies"] and not filtered["origins"]:
            raise NeedsLogin(platform, account, "Saved state is empty or expired")
        return filtered

    def cookies(self, platform: str, account: str | None, *, required=False) -> httpx.Cookies:
        # Playwright uses a leading dot for domain cookies; otherwise the cookie
        # belongs to that exact host. Preserve browser security/expiry semantics.
        jar = httpx.Cookies(CookieJar(policy=DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain)))
        for cookie in self.state(platform, account, required=required)["cookies"]:
            domain = cookie["domain"]
            expires = cookie.get("expires", -1)
            jar.jar.set_cookie(Cookie(version=0, name=cookie["name"], value=cookie["value"],
                port=None, port_specified=False, domain=domain,
                domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                path=cookie.get("path", "/"), path_specified=True,
                secure=bool(cookie.get("secure", False)), expires=expires if expires > 0 else None,
                discard=expires <= 0, comment=None, comment_url=None,
                rest={"HttpOnly": cookie.get("httpOnly", False), "SameSite": cookie.get("sameSite")},
                rfc2109=False))
        return jar

    def list(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((self.paths.root / "state/profiles").glob("*/*/account.json"))]

    def revoke(self, platform: str, account: str) -> dict:
        folder = self.paths.profile(platform, account)
        with self._locked(folder):
            info = self.info(platform, account)
            for name in ("storage_state.private", "storage_state.json"):
                (folder / name).unlink(missing_ok=True)
            info.update(status="revoked", cookie_count=0, revoked_at=utcnow(),session_revision=uuid.uuid4().hex)
            info.pop('validation',None)
            self._write_info(folder, info)
        return info


class ProxyStore:
    """Explicit supplied endpoints, selected by name; no provider purchase or identity rotation."""
    def __init__(self, paths: AppPaths):
        self.path = paths.root / "state/proxies.private"

    def _all(self):
        return read_private(self.path) if self.path.is_file() else {}

    def save(self, name: str, url: str) -> dict:
        safe_name(name)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            raise ValueError("Expected an HTTP/HTTPS/SOCKS5 proxy endpoint")
        values = self._all()
        values[name] = url
        write_private(self.path, values)
        return {"name": name, "scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port}

    def get(self, name: str | None) -> str | None:
        if name is None:
            return None
        return self._all()[safe_name(name)]

    def list(self) -> list[dict]:
        return [{"name": name, "scheme": urlsplit(url).scheme, "host": urlsplit(url).hostname,
                 "port": urlsplit(url).port} for name, url in self._all().items()]
