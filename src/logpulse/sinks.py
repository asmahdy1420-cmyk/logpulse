"""Output sinks: a console table, JSON lines and PostgreSQL."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import IO, Any, Protocol

from logpulse.anomaly import Alert
from logpulse.models import WindowMetrics, iso


class Sink(Protocol):
    def write_metrics(self, metrics: WindowMetrics) -> None: ...

    def write_alert(self, alert: Alert) -> None: ...

    def close(self) -> None: ...


def _fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}ms"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"  # pragma: no cover


class TableSink:
    """Human-readable block per window, written to a text stream (stdout by default)."""

    def __init__(self, stream: IO[str] | None = None, top: int = 5) -> None:
        self.stream = stream or sys.stdout
        self.top = top

    def write_metrics(self, m: WindowMetrics) -> None:
        w = self.stream.write
        w(f"\n== {iso(m.start)} -> {iso(m.end)} ({m.duration:g}s) ==\n")
        w(
            f"  requests {m.requests:<8} rps {m.rps:<8.2f} 5xx {m.error_rate:6.2%}   "
            f"4xx {m.client_error_rate:6.2%}   sent {_fmt_bytes(m.bytes_sent)}\n"
        )
        w(
            f"  latency  p50 {_fmt_ms(m.p50_latency_ms):>10}  p95 {_fmt_ms(m.p95_latency_ms):>10}"
            f"  p99 {_fmt_ms(m.p99_latency_ms):>10}\n"
        )
        w("  status   " + "  ".join(f"{k} {v}" for k, v in m.status_classes.items()) + "\n")
        paths = m.top_paths[: self.top]
        ips = m.top_ips[: self.top]
        if paths or ips:
            w(f"  {'top paths':<40} {'count':>7}   {'top ips':<18} {'count':>7}\n")
        for i in range(max(len(paths), len(ips))):
            p, pc = paths[i] if i < len(paths) else ("", "")
            ip, ic = ips[i] if i < len(ips) else ("", "")
            w(f"  {p[:40]:<40} {pc!s:>7}   {ip:<18} {ic!s:>7}\n")
        self.stream.flush()

    def write_alert(self, alert: Alert) -> None:
        self.stream.write(f"  !! ALERT [{alert.severity}] {alert.message}\n")
        self.stream.flush()

    def close(self) -> None:
        self.stream.flush()


class JsonLinesSink:
    """One JSON object per line: ``{"type": "metrics", ...}`` or ``{"type": "alert", ...}``."""

    def __init__(self, target: str | IO[str] = "-") -> None:
        if isinstance(target, str):
            self._owned = target != "-"
            self.stream: IO[str] = (
                open(target, "a", encoding="utf-8")  # noqa: SIM115
                if self._owned
                else sys.stdout
            )
        else:
            self._owned = False
            self.stream = target

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.stream.write(json.dumps({"type": kind, **payload}, separators=(",", ":")) + "\n")
        self.stream.flush()

    def write_metrics(self, metrics: WindowMetrics) -> None:
        self._emit("metrics", metrics.to_dict())

    def write_alert(self, alert: Alert) -> None:
        self._emit("alert", alert.to_dict())

    def close(self) -> None:
        if self._owned:
            self.stream.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS logpulse_windows (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    requests       INTEGER NOT NULL,
    rps            DOUBLE PRECISION NOT NULL,
    error_rate     DOUBLE PRECISION NOT NULL,
    p50_ms         DOUBLE PRECISION,
    p95_ms         DOUBLE PRECISION,
    p99_ms         DOUBLE PRECISION,
    bytes_sent     BIGINT NOT NULL,
    status_codes   JSONB NOT NULL,
    top_paths      JSONB NOT NULL,
    top_ips        JSONB NOT NULL,
    inserted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, window_start, window_end)
);
CREATE TABLE IF NOT EXISTS logpulse_alerts (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    metric         TEXT NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    baseline       DOUBLE PRECISION NOT NULL,
    zscore         DOUBLE PRECISION NOT NULL,
    severity       TEXT NOT NULL,
    inserted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS logpulse_alerts_window_end_idx ON logpulse_alerts (window_end);
"""

INSERT_WINDOW_SQL = """
INSERT INTO logpulse_windows (source, window_start, window_end, requests, rps, error_rate,
    p50_ms, p95_ms, p99_ms, bytes_sent, status_codes, top_paths, top_ips)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
ON CONFLICT (source, window_start, window_end) DO NOTHING
"""

INSERT_ALERT_SQL = """
INSERT INTO logpulse_alerts (source, window_start, window_end, metric, value, baseline,
    zscore, severity)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class PostgresSink:
    """Buffer rows and write them in batches with psycopg 3 ``executemany``.

    Re-processing the same log file is idempotent for windows thanks to the
    ``(source, window_start, window_end)`` unique key. Pass ``connection`` to reuse an
    existing psycopg connection (the sink then does not close it).
    """

    def __init__(
        self,
        dsn: str | None = None,
        connection: Any = None,
        source: str = "default",
        batch_size: int = 50,
        create_schema: bool = True,
    ) -> None:
        if connection is None:
            if dsn is None:
                raise ValueError("either dsn or connection is required")
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise RuntimeError(
                    "PostgreSQL sink needs psycopg: pip install 'logpulse[postgres]'"
                ) from exc
            connection = psycopg.connect(dsn)
            self._owns_connection = True
        else:
            self._owns_connection = False
        self.conn = connection
        self.source = source
        self.batch_size = batch_size
        self._windows: list[tuple[Any, ...]] = []
        self._alerts: list[tuple[Any, ...]] = []
        if create_schema:
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            self.conn.commit()

    def write_metrics(self, m: WindowMetrics) -> None:
        self._windows.append(
            (
                self.source,
                _dt(m.start),
                _dt(m.end),
                m.requests,
                m.rps,
                m.error_rate,
                m.p50_latency_ms,
                m.p95_latency_ms,
                m.p99_latency_ms,
                m.bytes_sent,
                json.dumps(m.status_codes),
                json.dumps([{"path": p, "count": c} for p, c in m.top_paths]),
                json.dumps([{"ip": ip, "count": c} for ip, c in m.top_ips]),
            )
        )
        self._maybe_flush()

    def write_alert(self, a: Alert) -> None:
        self._alerts.append(
            (
                self.source,
                _dt(a.window_start),
                _dt(a.window_end),
                a.metric,
                a.value,
                a.baseline,
                a.zscore,
                a.severity,
            )
        )
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if len(self._windows) + len(self._alerts) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._windows and not self._alerts:
            return
        with self.conn.cursor() as cur:
            if self._windows:
                cur.executemany(INSERT_WINDOW_SQL, self._windows)
            if self._alerts:
                cur.executemany(INSERT_ALERT_SQL, self._alerts)
        self.conn.commit()
        self._windows.clear()
        self._alerts.clear()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._owns_connection:
                self.conn.close()
