"""HTTP boundary for CookieBridge, deliberately separate from job payloads."""
from __future__ import annotations

import ipaddress
import json

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..core.accounts import AccountStore
from ..core.cookiebridge import BridgeError, CookieBridge, EXTENSION_ORIGIN

CAPABILITIES = {"implemented": ["chrome_mv3_optional_site_permission", "one_shot_local_pairing",
    "selected_account_dpapi_import", "pairing_revocation"],
    "pending": ["authenticated_platform_session_validation", "partitioned_cookie_transfer"]}


def register_routes(app, paths, store=None, runner=None):
    bridge = CookieBridge(AccountStore(paths))
    app.state.cookiebridge = bridge

    async def body(request, limit=1048576):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise BridgeError("JSON content type required", 415)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise BridgeError("Transfer exceeds size limit", 413)
        try:
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise BridgeError("Invalid JSON object", 422) from None

    @app.exception_handler(BridgeError)
    async def bridge_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.get("/api/bridge/status")
    async def status():
        return bridge.status()

    @app.post("/api/bridge/pairings", status_code=201)
    async def create(request: Request):
        value = await body(request, 4096)
        if set(value) - {"platform", "account", "site"} or not all(isinstance(value.get(key), str) for key in ("platform", "account")):
            raise BridgeError("Select a platform and account", 422)
        return bridge.create(value["platform"], value["account"], value.get("site"))

    @app.delete("/api/bridge/pairings/{pairing_id}")
    async def revoke(pairing_id: str):
        return bridge.revoke(pairing_id)

    @app.api_route("/bridge/v1/{operation}", methods=["POST", "OPTIONS"])
    async def transfer(operation: str, request: Request):
        origin = request.headers.get("origin", "")
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"} or not EXTENSION_ORIGIN.fullmatch(origin):
            return JSONResponse({"detail": "Local Chrome extension origin required"}, status_code=403)
        headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin", "Cache-Control": "no-store",
                   "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type, X-Bridge-Token",
                   "Access-Control-Allow-Private-Network": "true"}
        if operation not in {"pair", "import", "revoke"}:
            return JSONResponse({"detail": "Unknown bridge operation"}, status_code=404, headers=headers)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=headers)
        try:
            if operation == "pair":
                value = await body(request, 4096)
                result = bridge.pair(value.get("code"), origin)
            else:
                token = request.headers.get("x-bridge-token", "")
                authorized = bridge.authorize(token, origin)
                if operation == "revoke":
                    result = bridge.revoke(authorized["id"])
                else:
                    result = bridge.accept(token, origin, await body(request))
            return JSONResponse(result, headers=headers)
        except BridgeError as error:
            return JSONResponse({"detail": str(error)}, status_code=error.status, headers=headers)
        except Exception:
            # Never echo a validation error, cookie value, or persistence exception.
            return JSONResponse({"detail": "Transfer failed; no session validity is claimed"}, status_code=422, headers=headers)

    return bridge
