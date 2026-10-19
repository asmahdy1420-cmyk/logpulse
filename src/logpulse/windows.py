"""Event-time tumbling and sliding windows built from mergeable panes.

Time is cut into *panes* of ``slide`` seconds. A window of ``size`` seconds is the union of
``size / slide`` consecutive panes, so a tumbling window is the special case
``slide == size``. Each pane holds mergeable state (counters, a t-digest and two
heavy-hitter sketches); closing a sliding window merges its panes instead of re-reading
records. A record is processed once, whatever the overlap.

Windows close when the *watermark* (the largest event time seen minus the allowed
lateness) passes their end. Records for panes that have already closed are dropped and
counted in ``late_dropped``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable

from logpulse.models import LogRecord, WindowMetrics
from logpulse.sketches import HeavyHitters, TDigest


class PaneStats:
    """Mergeable aggregate for a slice of time."""

    __slots__ = ("bytes_sent", "ips", "latency", "paths", "requests", "status", "top_k")

    def __init__(
        self, top_k: int = 10, cms_width: int = 1024, cms_depth: int = 4, compression: float = 100.0
    ) -> None:
        self.top_k = top_k
        self.requests = 0
        self.bytes_sent = 0
        self.status: Counter[int] = Counter()
        self.latency = TDigest(compression)
        self.paths = HeavyHitters(top_k, cms_width, cms_depth, seed=1)
        self.ips = HeavyHitters(top_k, cms_width, cms_depth, seed=2)

    def add(self, record: LogRecord) -> None:
        self.requests += 1
        self.bytes_sent += record.bytes_sent
        self.status[record.status] += 1
        if record.latency_ms is not None:
            self.latency.add(record.latency_ms)
        self.paths.add(record.path)
        self.ips.add(record.ip)

    def merge(self, other: PaneStats) -> PaneStats:
        self.requests += other.requests
        self.bytes_sent += other.bytes_sent
        self.status.update(other.status)
        self.latency.merge(other.latency)
        self.paths.merge(other.paths)
        self.ips.merge(other.ips)
        return self

    def to_metrics(self, start: float, end: float) -> WindowMetrics:
        n = self.requests
        classes: Counter[str] = Counter()
        for code, count in self.status.items():
            classes[f"{code // 100}xx"] += count
        has_latency = self.latency.count > 0

        def q(p: float) -> float | None:
            return round(self.latency.quantile(p), 3) if has_latency else None

        return WindowMetrics(
            start=start,
            end=end,
            requests=n,
            rps=n / (end - start),
            error_rate=classes["5xx"] / n if n else 0.0,
            client_error_rate=classes["4xx"] / n if n else 0.0,
            bytes_sent=self.bytes_sent,
            status_classes=dict(sorted(classes.items())),
            status_codes={str(k): v for k, v in sorted(self.status.items())},
            p50_latency_ms=q(0.50),
            p95_latency_ms=q(0.95),
            p99_latency_ms=q(0.99),
            top_paths=self.paths.top(),
            top_ips=self.ips.top(),
        )


class Windower:
    def __init__(
        self,
        size: float,
        slide: float | None = None,
        allowed_lateness: float = 0.0,
        stats_factory: Callable[[], PaneStats] | None = None,
    ) -> None:
        slide = size if slide is None else slide
        if size <= 0 or slide <= 0:
            raise ValueError("window size and slide must be positive")
        panes = size / slide
        if slide > size or abs(panes - round(panes)) > 1e-9:
            raise ValueError("window size must be a whole multiple of the slide")
        self.size = float(size)
        self.slide = float(slide)
        self.allowed_lateness = float(allowed_lateness)
        self.panes_per_window = round(panes)
        self._factory = stats_factory or PaneStats
        self._panes: dict[int, PaneStats] = {}
        self._next_end: int | None = None  # pane index where the next window ends
        self._closed_before: int | None = None  # panes below this index are sealed
        self._max_ts = -math.inf
        self.late_dropped = 0

    @property
    def is_tumbling(self) -> bool:
        return self.panes_per_window == 1

    @property
    def watermark(self) -> float:
        return self._max_ts - self.allowed_lateness

    def add(self, record: LogRecord) -> list[WindowMetrics]:
        idx = math.floor(record.timestamp / self.slide)
        if self._closed_before is not None and idx < self._closed_before:
            self.late_dropped += 1
            return []
        pane = self._panes.get(idx)
        if pane is None:
            pane = self._panes[idx] = self._factory()
        pane.add(record)
        if self._next_end is None or idx + 1 < self._next_end:
            self._next_end = idx + 1
        if record.timestamp > self._max_ts:
            self._max_ts = record.timestamp
        return self.advance(self.watermark)

    def advance(self, watermark: float) -> list[WindowMetrics]:
        """Close every window whose end is at or before ``watermark``."""
        closed: list[WindowMetrics] = []
        while self._next_end is not None and self._next_end * self.slide <= watermark:
            result = self._close(self._next_end)
            if result is not None:
                closed.append(result)
        return closed

    def flush(self) -> list[WindowMetrics]:
        """Close all remaining windows that end at or before the last pane with data."""
        if not self._panes:
            return []
        last = max(self._panes)
        closed = self.advance((last + 1) * self.slide)
        self._panes.clear()
        self._next_end = None
        return closed

    def _close(self, end: int) -> WindowMetrics | None:
        start = end - self.panes_per_window
        members = [self._panes[i] for i in sorted(self._panes) if start <= i < end]
        result = None
        if members:
            if len(members) == 1 and self.is_tumbling:
                merged = members[0]
            else:
                merged = self._factory()
                for pane in members:
                    merged.merge(pane)
            result = merged.to_metrics(start * self.slide, end * self.slide)

        self._closed_before = end
        keep_from = end + 1 - self.panes_per_window
        for i in [i for i in self._panes if i < keep_from]:
            del self._panes[i]
        if self._panes:
            self._next_end = max(end + 1, min(self._panes) + 1)
        else:
            self._next_end = None
        return result
