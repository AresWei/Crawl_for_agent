from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Record:
    source: str
    kind: str
    native_id: str
    url: str
    payload: dict[str, Any]
    observed_at: str = field(default_factory=utcnow)
    source_date: str | None = None
    source_date_field: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) and item.strip() for item in (self.source, self.kind, self.native_id)):
            raise ValueError("Record source, kind and native_id must be nonempty strings")
        if not isinstance(self.payload, dict) or not isinstance(self.coverage, dict):
            raise ValueError("Record payload and coverage must be objects")
        if self.source_date is not None and not self.source_date_field:
            raise ValueError("A source date must identify its source field")
        canonical_json(self.payload)
        canonical_json(self.coverage)

    @property
    def key(self) -> str:
        value = canonical_json([self.source, self.kind, self.native_id])
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        value = canonical_json({"payload": self.payload, "source_date": self.source_date,
                                "source_date_field": self.source_date_field})
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
