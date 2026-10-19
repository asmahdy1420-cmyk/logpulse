from __future__ import annotations

import io
import json
from typing import Any

from logpulse.anomaly import Alert
from logpulse.models import WindowMetrics
from logpulse.sinks import JsonLinesSink, PostgresSink, TableSink


def metrics(i: int = 0) -> WindowMetrics:
    return WindowMetrics(
        start=i * 60.0,
        end=(i + 1) * 60.0,
        requests=10,
        rps=10 / 60,
        error_rate=0.1,
        client_error_rate=0.0,
        bytes_sent=2048,
        status_classes={"2xx": 9, "5xx": 1},
        status_codes={"200": 9, "500": 1},
        p50_latency_ms=12.0,
        p95_latency_ms=40.0,
        p99_latency_ms=55.0,
        top_paths=[("/a", 7), ("/b", 3)],
        top_ips=[("1.1.1.1", 10)],
    )


ALERT = Alert("error_rate", 0.3, 0.01, 9.5, "critical", 0.0, 60.0)


class FakeCursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.log.append(("execute", sql))

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.log.append(("executemany", (sql, list(rows))))


class FakeConnection:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.log)

    def commit(self) -> None:
        self.commits += 1


def test_jsonl_sink_writes_typed_records() -> None:
    buf = io.StringIO()
    sink = JsonLinesSink(buf)
    sink.write_metrics(metrics())
    sink.write_alert(ALERT)
    first, second = (json.loads(line) for line in buf.getvalue().splitlines())
    assert first["type"] == "metrics"
    assert first["top_paths"][0] == {"path": "/a", "count": 7}
    assert first["end_iso"] == "1970-01-01T00:01:00Z"
    assert second["type"] == "alert"
    assert second["severity"] == "critical"


def test_table_sink_renders() -> None:
    buf = io.StringIO()
    sink = TableSink(buf)
    sink.write_metrics(metrics())
    sink.write_alert(ALERT)
    out = buf.getvalue()
    assert "requests 10" in out
    assert "p99     55.0ms" in out
    assert "/a" in out
    assert "ALERT [critical]" in out


def test_postgres_sink_batches_inserts() -> None:
    conn = FakeConnection()
    sink = PostgresSink(connection=conn, source="test", batch_size=3)
    assert conn.log[0][0] == "execute"  # schema
    sink.write_metrics(metrics(0))
    sink.write_metrics(metrics(1))
    assert len(conn.log) == 1  # still buffered
    sink.write_alert(ALERT)  # third row reaches the batch size
    batches = [entry for entry in conn.log if entry[0] == "executemany"]
    assert len(batches) == 2  # one statement per table, two window rows in one call
    window_sql, window_rows = batches[0][1]
    assert "ON CONFLICT" in window_sql
    assert len(window_rows) == 2
    assert window_rows[0][0] == "test"
    assert json.loads(window_rows[0][11]) == [
        {"path": "/a", "count": 7},
        {"path": "/b", "count": 3},
    ]
    sink.write_metrics(metrics(2))
    sink.close()  # flushes the remainder
    assert len([e for e in conn.log if e[0] == "executemany"]) == 3
    assert conn.commits == 3
