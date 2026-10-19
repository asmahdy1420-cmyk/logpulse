from __future__ import annotations

from pathlib import Path

import pytest

from logpulse.models import LogRecord

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_log() -> Path:
    return FIXTURES / "access_sample.log"


def make_record(
    ts: float,
    status: int = 200,
    path: str = "/",
    ip: str = "10.0.0.1",
    latency_ms: float | None = 10.0,
) -> LogRecord:
    return LogRecord(
        timestamp=ts,
        ip=ip,
        method="GET",
        path=path,
        status=status,
        bytes_sent=100,
        latency_ms=latency_ms,
    )
