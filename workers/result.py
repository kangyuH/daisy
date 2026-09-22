from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WorkerResult:
    status: str  # ok | skip | retry | fail
    error: Optional[str] = None
    busy: bool = False

    @classmethod
    def ok(cls) -> "WorkerResult":
        return cls("ok")

    @classmethod
    def skip(cls, reason: str = "") -> "WorkerResult":
        return cls("skip", error=reason or None)

    @classmethod
    def retry(cls, error: str, *, busy: bool = False) -> "WorkerResult":
        return cls("retry", error=error, busy=busy)

    @classmethod
    def fail(cls, error: str) -> "WorkerResult":
        return cls("fail", error=error)
