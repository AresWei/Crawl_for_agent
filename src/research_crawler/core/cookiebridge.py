"""One-shot, selected-site Chrome cookie transfer; secrets never enter job storage."""
from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from .accounts import AccountStore, PLATFORM_SITES

EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
SITES = {platform: urlsplit(value[0]).hostname for platform, value in PLATFORM_SITES.items()}


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class CookieBridge:
    def __init__(self, accounts: AccountStore, *, clock=time.time):
        self.accounts, self.clock = accounts, clock
        self._pairings: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _digest(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def _expire(self):
        now = self.clock()
        for item in self._pairings.values():
            if item["status"] in {"pending", "paired"} and item["expires_at"] <= now:
                item["status"] = "expired"
                item.pop("code_hash", None)
                item.pop("token_hash", None)
        # In-memory diagnostic history is bounded; no secret persistence.
        if len(self._pairings) > 64:
            for key in list(self._pairings):
                if len(self._pairings) <= 64:
                    break
                if self._pairings[key]["status"] not in {"pending", "paired"}:
                    del self._pairings[key]

    @staticmethod
    def public(item):
        return {key: item[key] for key in ("id", "platform", "account", "site", "status", "expires_at")}

    def create(self, platform: str, account: str, site: str | None = None) -> dict:
        if os.name != "nt":
            raise BridgeError("CookieBridge requires Windows DPAPI; plaintext fallback is disabled", 503)
        if platform not in SITES or (site is not None and site != SITES[platform]):
            raise BridgeError("Choose a supported primary site", 422)
        try:
            self.accounts.info(platform, account)
        except (KeyError, ValueError, TypeError):
            raise BridgeError("Create and select the target account first", 422) from None
        with self._lock:
            self._expire()
            if sum(item["status"] in {"pending", "paired"} for item in self._pairings.values()) >= 16:
                raise BridgeError("Revoke an existing pairing first", 429)
            code = secrets.token_hex(10).upper()
            item = {"id": secrets.token_urlsafe(16), "platform": platform, "account": account,
                    "site": SITES[platform], "status": "pending", "expires_at": self.clock() + 120,
                    "code_hash": self._digest(code)}
            self._pairings[item["id"]] = item
            return {**self.public(item), "code": code}

    def pair(self, code: str, origin: str) -> dict:
        if not EXTENSION_ORIGIN.fullmatch(origin) or not isinstance(code, str) or not re.fullmatch(r"[A-Fa-f0-9]{20}", code):
            raise BridgeError("Invalid or expired pairing code", 401)
        with self._lock:
            self._expire()
            digest = self._digest(code.upper())
            item = next((value for value in self._pairings.values() if value["status"] == "pending"
                         and secrets.compare_digest(value["code_hash"], digest)), None)
            if item is None:
                raise BridgeError("Invalid or expired pairing code", 401)
            token = secrets.token_urlsafe(32)
            item.update(status="paired", origin=origin, token_hash=self._digest(token), expires_at=self.clock() + 600)
            item.pop("code_hash")
            return {**self.public(item), "token": token}

    def authorize(self, token: str, origin: str) -> dict:
        with self._lock:
            self._expire()
            digest = self._digest(token)
            item = next((value for value in self._pairings.values() if value["status"] == "paired"
                         and value.get("origin") == origin and secrets.compare_digest(value["token_hash"], digest)), None)
            if item is None:
                raise BridgeError("Pairing expired, used or revoked; pair again", 401)
            return self.public(item)

    def accept(self, token: str, origin: str, payload: dict) -> dict:
        with self._lock:
            item = self._pairings[self.authorize(token, origin)["id"]]
            if not isinstance(payload, dict) or set(payload) != {"site", "cookies"} or payload["site"] != item["site"]:
                raise BridgeError("Transfer does not match the selected site", 422)
            raw = payload["cookies"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= 300:
                raise BridgeError("Expected 1 to 300 selected-site cookies", 422)
            cookies = [self._cookie(cookie, item["site"]) for cookie in raw]
            # AccountStore provides atomic DPAPI storage, shared with normal login.
            saved = self.accounts.save(item["platform"], item["account"], {"cookies": cookies, "origins": []})
            item.update(status="imported")
            item.pop("token_hash", None)
            return {**self.public(item), "cookie_count": saved["cookie_count"], "session_validity": "saved_unverified"}

    def _cookie(self, raw, site):
        if not isinstance(raw, dict):
            raise BridgeError("Invalid cookie structure", 422)
        allowed = {"name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite"}
        if set(raw) - allowed or not {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"} <= set(raw):
            raise BridgeError("Unsupported cookie fields (partitioned cookies are not imported)", 422)
        domain = raw["domain"]
        if not isinstance(domain, str) or not re.fullmatch(r"\.?[a-z0-9]+(?:[.-][a-z0-9]+)*", domain):
            raise BridgeError("Invalid cookie domain", 422)
        # URL-scoped Chrome query: exact host-only cookies or parent domain cookies.
        host = domain.lstrip(".")
        if not (site == host or (domain.startswith(".") and site.endswith("." + host))):
            raise BridgeError("Cookie is outside the selected host", 422)
        if not any(host == base or host.endswith("." + base) for _, bases in PLATFORM_SITES.values() for base in bases):
            raise BridgeError("Cookie parent scope is too broad", 422)
        if raw["path"] != "/":
            raise BridgeError("Only selected-site root-path cookies are supported", 422)
        if any(not isinstance(raw[key], str) or len(raw[key]) > 16384 or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw[key]) for key in ("name", "value")):
            raise BridgeError("Invalid cookie text", 422)
        if not raw["name"] or any(char in raw["name"] for char in ";= \t"):
            raise BridgeError("Invalid cookie name", 422)
        if type(raw["secure"]) is not bool or type(raw["httpOnly"]) is not bool or raw["sameSite"] not in {"Strict", "Lax", "None"}:
            raise BridgeError("Invalid cookie flags", 422)
        expiry = raw["expires"]
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or (expiry != -1 and expiry <= self.clock()):
            raise BridgeError("Expired or invalid cookie", 422)
        if raw["sameSite"] == "None" and not raw["secure"]:
            raise BridgeError("SameSite=None requires Secure", 422)
        if raw["name"].startswith("__Secure-") and not raw["secure"]:
            raise BridgeError("Invalid secure cookie prefix", 422)
        if raw["name"].startswith("__Host-") and (not raw["secure"] or domain.startswith(".")):
            raise BridgeError("Invalid host cookie prefix", 422)
        return {key: raw[key] for key in allowed}

    def revoke(self, pairing_id: str) -> dict:
        with self._lock:
            if pairing_id not in self._pairings:
                raise BridgeError("Pairing not found", 404)
            item = self._pairings[pairing_id]
            item.update(status="revoked")
            item.pop("code_hash", None)
            item.pop("token_hash", None)
            return self.public(item)

    def revoke_account(self, platform: str, account: str) -> None:
        """Call when the owning account is revoked; invalidate outstanding grants."""
        with self._lock:
            for item in self._pairings.values():
                if item["platform"] == platform and item["account"] == account and item["status"] in {"pending", "paired"}:
                    self.revoke(item["id"])

    def status(self) -> dict:
        with self._lock:
            self._expire()
            return {"available": os.name == "nt", "sites": SITES,
                    "pairings": [self.public(item) for item in self._pairings.values()],
                    "protection": "windows_dpapi", "transfer": "one_shot", "scope": "primary_host_root_path"}
