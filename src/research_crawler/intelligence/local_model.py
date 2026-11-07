"""Bounded, authenticated loopback client for the separately managed model worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import httpx

from ..core.gpu import GPUCoordinator
from ..core.paths import AppPaths
from ..core.model_profiles import active_profile, profile_for_path


class LocalModelUnavailable(RuntimeError):
    """The local model worker is absent, loading, or not authenticated."""


@dataclass(frozen=True)
class ModelCompletion:
    text: str
    model: str
    usage: dict[str, Any]
    latency_ms: float
    finish_reason: str


class LocalModelClient:
    """No remote URLs, environment proxies, or paid-provider fallback.

    Use ``async with LocalModelClient(root) as model`` to share and close one
    HTTP connection pool. Cancellation propagates through HTTPX to the worker.
    Evidence selection, citations and secret redaction belong to the caller.
    """

    def __init__(self, project_root: Path | str | None = None, *, timeout: float = 120.0):
        self.root = AppPaths.discover(project_root).root
        self.token_path = self.root / "runtime/model-server/token"
        self._http = httpx.AsyncClient(
            base_url="http://127.0.0.1:8947", trust_env=False,
            timeout=httpx.Timeout(timeout, connect=3.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
        )

    async def __aenter__(self) -> "LocalModelClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_path.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise LocalModelUnavailable("Local model is not set up; run scripts/model-setup.ps1") from exc
        if not token:
            raise LocalModelUnavailable("Local model authentication token is empty")
        return {"Authorization": f"Bearer {token}"}

    async def health(self) -> dict[str, Any]:
        try:
            # A health check must not wait behind image generation or wake an idle
            # model. /health and /props are exempt from llama.cpp idle wakeup.
            async with GPUCoordinator(self.root).lease('status', wait_timeout=.05) as gpu:
                if gpu.language.parked_path.exists():
                    return {"available": False, "provider": "local-llama.cpp", "status": "suspended",
                            "configured": self.token_path.exists(), "reason": "Local language worker awaits restoration"}
                response = await self._http.get("/health", headers=self._headers(), timeout=3)
                response.raise_for_status()
                props = await self._http.get("/props", headers=self._headers(), timeout=3)
                props.raise_for_status()
                details=props.json()
                sleeping = bool(details.get('is_sleeping', False))
                actual=profile_for_path(self.root,details['model_path'])
                configured=active_profile(self.root)
                if actual['id']!=configured['id']:raise ValueError('Worker model differs from managed state')
                return {"available": True, "provider": "local-llama.cpp", "status": 'sleeping' if sleeping else 'ready',
                        "models": [{"id": actual['alias']}], 'model_id':actual['id'], 'model_revision':actual['revision']}
        except TimeoutError:
            return {"available": False, "provider": "local-llama.cpp", "status": "busy",
                    "configured": self.token_path.exists(), "reason": "GPU is reserved by another local workload"}
        except (httpx.HTTPError, LocalModelUnavailable, ValueError, KeyError, OSError) as exc:
            # Never expose HTTP request headers or token paths in status payloads.
            return {"available": False, "provider": "local-llama.cpp", "reason": type(exc).__name__}

    async def complete(
        self, messages: list[dict[str, str]], *, max_tokens: int = 512,
        temperature: float = 0.2, response_format: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        if not 1 <= max_tokens <= 1024:
            raise ValueError("max_tokens must be between 1 and 1024")
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not messages or len(messages) > 64:
            raise ValueError("Expected 1 to 64 messages")
        if any(m.get("role") not in {"system", "user", "assistant"} or not isinstance(m.get("content"), str) for m in messages):
            raise ValueError("Messages require supported roles and text content")
        if sum(len(m["content"]) for m in messages) > 12000:
            raise ValueError("Input exceeds the bounded local-model character budget")
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
            "top_p": 0.8, "top_k": 20, "min_p": 0,
            "stream": False, "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }
        if response_format is not None:
            payload["response_format"] = response_format
        started = time.perf_counter()
        try:
            async with GPUCoordinator(self.root).lease('language'):
                payload['model']=active_profile(self.root)['alias']
                response = await self._http.post("/v1/chat/completions", headers=self._headers(), json=payload)
            if response.status_code in {401, 403, 502, 503, 504}:
                raise LocalModelUnavailable(f"Local model worker returned HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise LocalModelUnavailable("Local model is unavailable or exceeded its time budget") from exc
        choice = data["choices"][0]
        if data.get('model')!=payload['model']:
            raise LocalModelUnavailable('Local model response identity differs from the selected worker')
        text = choice["message"].get("content")
        if not isinstance(text, str) or not text.strip():
            raise LocalModelUnavailable("Local model returned no answer text")
        return ModelCompletion(
            text=text.strip(), model=data['model'],
            usage=data.get("usage", {}), latency_ms=round((time.perf_counter() - started) * 1000, 3),
            finish_reason=choice.get("finish_reason", "unknown"),
        )
