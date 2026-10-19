"""EWMA baseline + z-score anomaly detection over closed windows.

For every watched metric we keep an exponentially weighted mean and variance:

    diff  = x - mean
    mean += alpha * diff
    var   = (1 - alpha) * (var + alpha * diff^2)

A new value is scored against the baseline *before* it is folded in:
``z = (x - mean) / max(sqrt(var), min_std)``. ``min_std`` stops a perfectly flat history
(error rate 0.0 for an hour) from turning the first single error into an infinite z-score.
Each update is O(1) time and memory per metric.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from logpulse.models import WindowMetrics, iso

DEFAULT_MIN_STD = {"error_rate": 0.01, "p95_latency_ms": 5.0, "p99_latency_ms": 5.0, "rps": 1.0}


@dataclass
class Alert:
    metric: str
    value: float
    baseline: float
    zscore: float
    severity: str  # "warning" | "critical"
    window_start: float
    window_end: float

    @property
    def message(self) -> str:
        return (
            f"{self.metric}={self.value:.4g} vs baseline {self.baseline:.4g} "
            f"(z={self.zscore:.1f}) in window ending {iso(self.window_end)}"
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["window_end_iso"] = iso(self.window_end)
        data["message"] = self.message
        return data


class EWMAStat:
    __slots__ = ("alpha", "mean", "n", "var")

    def __init__(self, alpha: float) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be within (0, 1]")
        self.alpha = alpha
        self.mean = 0.0
        self.var = 0.0
        self.n = 0

    def zscore(self, x: float, min_std: float = 0.0) -> float:
        std = max(math.sqrt(self.var), min_std)
        if std == 0:
            return 0.0 if x == self.mean else math.inf
        return (x - self.mean) / std

    def update(self, x: float) -> None:
        if self.n == 0:
            self.mean = x
        else:
            diff = x - self.mean
            incr = self.alpha * diff
            self.mean += incr
            self.var = (1 - self.alpha) * (self.var + diff * incr)
        self.n += 1


class AnomalyDetector:
    """Raise alerts when a metric jumps above its EWMA baseline.

    ``direction`` is ``"up"`` (default: more errors, slower responses are bad), ``"down"``
    or ``"both"``. Windows with fewer than ``min_requests`` requests are not scored, because
    an error rate over three requests is noise.
    """

    def __init__(
        self,
        metrics: tuple[str, ...] = ("error_rate", "p95_latency_ms"),
        alpha: float = 0.3,
        threshold: float = 3.0,
        warmup: int = 5,
        min_requests: int = 20,
        min_std: dict[str, float] | None = None,
        direction: str = "up",
    ) -> None:
        if direction not in {"up", "down", "both"}:
            raise ValueError("direction must be 'up', 'down' or 'both'")
        self.metrics = metrics
        self.threshold = threshold
        self.warmup = warmup
        self.min_requests = min_requests
        self.direction = direction
        self.min_std = {**DEFAULT_MIN_STD, **(min_std or {})}
        self._stats = {m: EWMAStat(alpha) for m in metrics}

    def baseline(self, metric: str) -> tuple[float, float]:
        stat = self._stats[metric]
        return stat.mean, math.sqrt(stat.var)

    def observe(self, window: WindowMetrics) -> list[Alert]:
        if window.requests < self.min_requests:
            return []
        alerts: list[Alert] = []
        for metric in self.metrics:
            value = getattr(window, metric, None)
            if value is None:
                continue
            stat = self._stats[metric]
            if stat.n >= self.warmup:
                z = stat.zscore(value, self.min_std.get(metric, 0.0))
                if self._is_anomalous(z):
                    severity = "critical" if abs(z) >= 2 * self.threshold else "warning"
                    alerts.append(
                        Alert(
                            metric, float(value), stat.mean, z, severity, window.start, window.end
                        )
                    )
            # Anomalies still update the baseline, so a lasting level shift becomes the new
            # normal after a few windows instead of alerting forever.
            stat.update(float(value))
        return alerts

    def _is_anomalous(self, z: float) -> bool:
        if self.direction == "up":
            return z >= self.threshold
        if self.direction == "down":
            return z <= -self.threshold
        return abs(z) >= self.threshold
