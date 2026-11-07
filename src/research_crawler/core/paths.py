from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


def safe_name(value: str) -> str:
    if not _NAME.fullmatch(value) or value in {".", ".."}:
        raise ValueError("Expected a short name containing letters, digits, dots, dashes or underscores")
    return value


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def discover(cls, root: str | Path | None = None) -> "AppPaths":
        value = root or os.environ.get("RESEARCH_CRAWLER_HOME")
        if value is None:
            value = Path(__file__).resolve().parents[3]
        return cls(Path(value).expanduser().resolve())

    def initialize(self) -> None:
        for folder in ("state", "state/profiles", "runs", "cache", "temp", "models"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    @property
    def database(self) -> Path:
        return self.root / "state" / "research.db"

    def run(self, job_id: str) -> Path:
        return self.root / "runs" / safe_name(job_id)

    def profile(self, platform: str, account: str) -> Path:
        return self.root / "state" / "profiles" / safe_name(platform) / safe_name(account)

    def child_environment(self) -> dict[str, str]:
        """Return process-scoped paths without editing global OS or Codex settings."""
        env = dict(os.environ)
        for key, relative in {
            "RESEARCH_CRAWLER_HOME": ".", "TEMP": "temp", "TMP": "temp",
            "PIP_CACHE_DIR": "cache/pip", "UV_CACHE_DIR": "cache/uv",
            "PLAYWRIGHT_BROWSERS_PATH": "runtime/browsers",
            "CRAWL4_AI_BASE_DIRECTORY": "cache/web", "HF_HOME": "cache/huggingface",
            "TORCH_HOME": "cache/torch", "OLLAMA_MODELS": "models/ollama",
        }.items():
            env[key] = str(self.root / relative)
        return env
