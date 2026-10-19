from __future__ import annotations

from logpulse.anomaly import AnomalyDetector, EWMAStat
from logpulse.models import WindowMetrics


def window(
    i: int, error_rate: float = 0.01, p95: float | None = 80.0, requests: int = 1000
) -> WindowMetrics:
    return WindowMetrics(
        start=i * 60.0,
        end=(i + 1) * 60.0,
        requests=requests,
        rps=requests / 60,
        error_rate=error_rate,
        client_error_rate=0.0,
        bytes_sent=0,
        p95_latency_ms=p95,
    )


def test_ewma_converges_to_constant() -> None:
    stat = EWMAStat(alpha=0.5)
    for _ in range(50):
        stat.update(10.0)
    assert stat.mean == 10.0
    assert stat.var == 0.0
    assert stat.zscore(10.0) == 0.0


def test_error_rate_spike_triggers_critical_alert() -> None:
    detector = AnomalyDetector(warmup=5)
    alerts = []
    for i in range(20):
        alerts += detector.observe(window(i, error_rate=0.01 + (i % 2) * 0.002))
    assert alerts == []  # normal jitter stays quiet
    spike = detector.observe(window(20, error_rate=0.25))
    assert [a.metric for a in spike] == ["error_rate"]
    assert spike[0].severity == "critical"
    assert spike[0].zscore > 6
    assert "error_rate=0.25" in spike[0].message


def test_latency_spike_triggers_alert() -> None:
    detector = AnomalyDetector(warmup=5)
    for i in range(15):
        assert detector.observe(window(i, p95=80.0 + (i % 3))) == []
    alerts = detector.observe(window(15, p95=480.0))
    assert [a.metric for a in alerts] == ["p95_latency_ms"]


def test_no_alerts_during_warmup_or_small_windows() -> None:
    detector = AnomalyDetector(warmup=5, min_requests=20)
    assert detector.observe(window(0, error_rate=0.0)) == []
    assert detector.observe(window(1, error_rate=0.9)) == []  # still warming up
    for i in range(2, 10):
        detector.observe(window(i))
    assert detector.observe(window(10, error_rate=1.0, requests=3)) == []  # too few requests


def test_level_shift_is_absorbed() -> None:
    detector = AnomalyDetector(warmup=3, alpha=0.3)
    for i in range(10):
        detector.observe(window(i, p95=50.0))
    fired = [bool(detector.observe(window(10 + i, p95=200.0))) for i in range(30)]
    assert fired[0] is True
    assert fired[-1] is False  # the new level becomes the baseline


def test_down_direction() -> None:
    detector = AnomalyDetector(metrics=("rps",), direction="down", warmup=3)
    for i in range(10):
        detector.observe(window(i, requests=6000 + (i % 2) * 60))
    alerts = detector.observe(window(10, requests=600))
    assert len(alerts) == 1
    assert alerts[0].zscore < 0
