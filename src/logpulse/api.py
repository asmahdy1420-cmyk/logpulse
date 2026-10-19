"""A small read-only HTTP API over the metrics store (requires the ``api`` extra)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from logpulse import __version__
from logpulse.store import MetricsStore


def create_app(store: MetricsStore) -> FastAPI:
    app = FastAPI(
        title="logpulse",
        version=__version__,
        description="Current windowed metrics and alerts from a logpulse pipeline.",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "pipeline": store.pipeline_stats()}

    @app.get("/metrics")
    def latest_metrics() -> dict[str, Any]:
        latest = store.latest()
        if latest is None:
            raise HTTPException(status_code=404, detail="no window has closed yet")
        return latest.to_dict()

    @app.get("/metrics/history")
    def metrics_history(limit: int = Query(20, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [w.to_dict() for w in store.history(limit)]

    @app.get("/alerts")
    def alerts(limit: int = Query(50, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [a.to_dict() for a in store.alerts(limit)]

    return app
