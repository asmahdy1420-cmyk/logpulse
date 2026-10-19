"""Core data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class LogRecord:
    """One parsed request, normalised across log formats."""

    timestamp: float  # Unix epoch seconds (UTC)
    ip: str
    method: str
    path: str
    status: int
    bytes_sent: int = 0
    latency_ms: float | None = None
    user_agent: str = ""


@dataclass
class WindowMetrics:
    """Aggregated metrics for one closed window [start, end)."""

    start: float
    end: float
    requests: int
    rps: float
    error_rate: float  # share of 5xx responses
    client_error_rate: float  # share of 4xx responses
    bytes_sent: int
    status_classes: dict[str, int] = field(default_factory=dict)
    status_codes: dict[str, int] = field(default_factory=dict)
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    p99_latency_ms: float | None = None
    top_paths: list[tuple[str, int]] = field(default_factory=list)
    top_ips: list[tuple[str, int]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start_iso"] = iso(self.start)
        data["end_iso"] = iso(self.end)
        data["top_paths"] = [{"path": p, "count": c} for p, c in self.top_paths]
        data["top_ips"] = [{"ip": ip, "count": c} for ip, c in self.top_ips]
        return data


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
