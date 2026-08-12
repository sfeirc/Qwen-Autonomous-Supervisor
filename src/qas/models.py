from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(operation: str, exit_code: int | None, error: str) -> str:
    normalized = re.sub(r"\b[0-9a-f]{7,64}\b", "<sha>", error.lower())
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()[:2000]
    raw = canonical_json([operation, exit_code, normalized]).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    reason: str
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str
    session_id: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    exit_code: int
    duration_seconds: float
    output_tail: str


@dataclass(frozen=True)
class TickOutcome:
    tick_id: str
    run_id: str
    state: str
    success: bool
    reason: str
    review_passed: bool | None = None
