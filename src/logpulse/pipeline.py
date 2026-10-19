"""Wires parser -> windower -> anomaly detector -> sinks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass

from logpulse.anomaly import AnomalyDetector
from logpulse.models import WindowMetrics
from logpulse.parsers import Parser
from logpulse.sinks import Sink
from logpulse.windows import Windower

log = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    lines: int = 0
    parsed: int = 0
    parse_errors: int = 0
    late_dropped: int = 0
    windows: int = 0
    alerts: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        parser: Parser,
        windower: Windower,
        sinks: Sequence[Sink] = (),
        detector: AnomalyDetector | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """``clock`` enables wall-clock window closing on idle heartbeats (follow mode)."""
        self.parser = parser
        self.windower = windower
        self.sinks = list(sinks)
        self.detector = detector
        self.clock = clock
        self.stats = PipelineStats()

    def process_line(self, line: str) -> None:
        self.stats.lines += 1
        if not line.strip():
            return
        record = self.parser.parse(line)
        if record is None:
            self.stats.parse_errors += 1
            return
        self.stats.parsed += 1
        self._emit(self.windower.add(record))
        self.stats.late_dropped = self.windower.late_dropped

    def tick(self) -> None:
        """Idle heartbeat: close windows by wall clock when no new lines arrive."""
        if self.clock is not None:
            self._emit(self.windower.advance(self.clock() - self.windower.allowed_lateness))

    def run(self, lines: Iterable[str | None]) -> PipelineStats:
        try:
            for line in lines:
                if line is None:
                    self.tick()
                else:
                    self.process_line(line)
            self._emit(self.windower.flush())
        finally:
            self.close()
        return self.stats

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:  # one broken sink must not hide the others' data
                log.exception("failed to close sink %r", sink)

    def _emit(self, windows: list[WindowMetrics]) -> None:
        for window in windows:
            self.stats.windows += 1
            alerts = self.detector.observe(window) if self.detector else []
            self.stats.alerts += len(alerts)
            for sink in self.sinks:
                sink.write_metrics(window)
                for alert in alerts:
                    sink.write_alert(alert)
