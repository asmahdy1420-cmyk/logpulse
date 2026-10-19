from __future__ import annotations

import pytest

from logpulse.windows import Windower
from tests.conftest import make_record


def test_tumbling_window_boundaries() -> None:
    w = Windower(size=60)
    closed = []
    for ts in (0, 30, 59.999):
        closed += w.add(make_record(ts))
    assert closed == []  # nothing closes before the watermark reaches 60
    closed += w.add(make_record(60.0))  # the record at exactly 60 belongs to the next window
    assert len(closed) == 1
    first = closed[0]
    assert (first.start, first.end, first.requests) == (0.0, 60.0, 3)
    assert first.rps == pytest.approx(3 / 60)
    tail = w.flush()
    assert [(m.start, m.end, m.requests) for m in tail] == [(60.0, 120.0, 1)]


def test_sliding_windows_overlap() -> None:
    w = Windower(size=30, slide=10)
    closed = []
    for ts in range(0, 40):  # one request per second
        closed += w.add(make_record(float(ts)))
    closed += w.flush()
    spans = [(m.start, m.end, m.requests) for m in closed]
    assert spans == [
        (-20.0, 10.0, 10),  # warm-up windows only see part of the data
        (-10.0, 20.0, 20),
        (0.0, 30.0, 30),
        (10.0, 40.0, 30),
    ]


def test_gaps_skip_empty_windows() -> None:
    w = Windower(size=10)
    closed = w.add(make_record(1.0)) + w.add(make_record(1000.0)) + w.flush()
    assert [(m.start, m.requests) for m in closed] == [(0.0, 1), (1000.0, 1)]


def test_allowed_lateness_and_late_drops() -> None:
    w = Windower(size=10, allowed_lateness=5)
    closed = w.add(make_record(9.0)) + w.add(make_record(12.0))
    assert closed == []  # watermark 7 < 10
    closed += w.add(make_record(8.0))  # out of order but within lateness
    closed += w.add(make_record(15.5))  # watermark 10.5 closes [0, 10)
    assert [(m.start, m.requests) for m in closed] == [(0.0, 2)]
    w.add(make_record(3.0))  # its window has already closed
    assert w.late_dropped == 1


def test_window_metrics_content() -> None:
    w = Windower(size=60)
    statuses = [200] * 90 + [404] * 5 + [503] * 5
    for i, status in enumerate(statuses):
        w.add(make_record(i * 0.5, status=status, path=f"/p{i % 3}", latency_ms=float(i + 1)))
    (m,) = w.flush()
    assert m.requests == 100
    assert m.error_rate == pytest.approx(0.05)
    assert m.client_error_rate == pytest.approx(0.05)
    assert m.status_classes == {"2xx": 90, "4xx": 5, "5xx": 5}
    assert m.p50_latency_ms == pytest.approx(50.5, abs=1.0)
    assert m.p99_latency_ms == pytest.approx(99.0, abs=1.5)
    assert {p for p, _ in m.top_paths} == {"/p0", "/p1", "/p2"}


def test_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="multiple"):
        Windower(size=60, slide=25)
    with pytest.raises(ValueError, match="positive"):
        Windower(size=0)
