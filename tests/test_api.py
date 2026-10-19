from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from logpulse.anomaly import Alert
from logpulse.api import create_app
from logpulse.models import WindowMetrics
from logpulse.store import MetricsStore


def test_api_exposes_latest_metrics_history_and_alerts() -> None:
    store = MetricsStore()
    store.stats_provider = lambda: {"lines": 42}
    client = TestClient(create_app(store))

    assert client.get("/metrics").status_code == 404
    assert client.get("/health").json() == {"status": "ok", "pipeline": {"lines": 42}}

    for i in range(3):
        store.write_metrics(
            WindowMetrics(
                start=i * 60.0,
                end=(i + 1) * 60.0,
                requests=i + 1,
                rps=0.1,
                error_rate=0.0,
                client_error_rate=0.0,
                bytes_sent=0,
                top_paths=[("/a", i + 1)],
            )
        )
    store.write_alert(Alert("error_rate", 0.5, 0.01, 12.0, "critical", 120.0, 180.0))

    latest = client.get("/metrics").json()
    assert latest["requests"] == 3
    assert latest["top_paths"] == [{"path": "/a", "count": 3}]
    history = client.get("/metrics/history", params={"limit": 2}).json()
    assert [w["requests"] for w in history] == [2, 3]
    alerts = client.get("/alerts").json()
    assert alerts[0]["metric"] == "error_rate"
    assert client.get("/metrics/history", params={"limit": 0}).status_code == 422
