"""Thread-safe in-memory store of recent windows and alerts, used by the HTTP API."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from logpulse.anomaly import Alert
from logpulse.models import WindowMetrics


class MetricsStore:
    """Acts as a sink, so the pipeline feeds it like any other output."""

    def __init__(self, history: int = 120, max_alerts: int = 200) -> None:
        self._lock = threading.Lock()
        self._windows: deque[WindowMetrics] = deque(maxlen=history)
        self._alerts: deque[Alert] = deque(maxlen=max_alerts)
        self.stats_provider: Callable[[], dict[str, Any]] | None = None

    def write_metrics(self, metrics: WindowMetrics) -> None:
        with self._lock:
            self._windows.append(metrics)

    def write_alert(self, alert: Alert) -> None:
        with self._lock:
            self._alerts.append(alert)

    def close(self) -> None:
        return None

    def latest(self) -> WindowMetrics | None:
        with self._lock:
            return self._windows[-1] if self._windows else None

    def history(self, limit: int = 20) -> list[WindowMetrics]:
        with self._lock:
            items = list(self._windows)
        return items[-limit:] if limit > 0 else []

    def alerts(self, limit: int = 50) -> list[Alert]:
        with self._lock:
            items = list(self._alerts)
        return items[-limit:] if limit > 0 else []

    def pipeline_stats(self) -> dict[str, Any]:
        return self.stats_provider() if self.stats_provider else {}
