from __future__ import annotations

from datetime import datetime, timezone

import pytest

from logpulse.models import LogRecord
from logpulse.parsers import (
    AutoParser,
    JsonParser,
    NginxParser,
    available_parsers,
    get_parser,
    register_parser,
)

TEN_AM = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def test_nginx_combined_with_request_time() -> None:
    line = (
        '203.0.113.7 - - [19/Oct/2026:10:00:01 +0000] "GET /api/products?page=2 HTTP/1.1" '
        '200 5120 "-" "Mozilla/5.0" 0.042'
    )
    rec = NginxParser().parse(line)
    assert rec == LogRecord(
        timestamp=TEN_AM + 1,
        ip="203.0.113.7",
        method="GET",
        path="/api/products",
        status=200,
        bytes_sent=5120,
        latency_ms=pytest.approx(42.0),
        user_agent="Mozilla/5.0",
    )


def test_nginx_timezone_offset_and_missing_fields() -> None:
    parser = NginxParser()
    rec = parser.parse('1.2.3.4 - bob [19/Oct/2026:12:00:04 +0200] "GET / HTTP/2.0" 304 - "-" "x"')
    assert rec is not None
    assert rec.timestamp == TEN_AM + 4
    assert rec.bytes_sent == 0
    assert rec.latency_ms is None
    # Common log format (no referer/agent) is accepted too.
    assert (
        parser.parse('1.2.3.4 - - [19/Oct/2026:10:00:00 +0000] "GET / HTTP/1.0" 200 1') is not None
    )
    assert parser.parse("garbage") is None


def test_json_aliases_and_units() -> None:
    parser = JsonParser()
    a = parser.parse(
        '{"timestamp": "2026-10-19T10:00:00Z", "status": 500, "path": "/x?y=1",'
        ' "ip": "9.9.9.9", "latency_ms": 12.5}'
    )
    b = parser.parse(
        f'{{"ts": {int(TEN_AM * 1000)}, "status_code": "201", "uri": "/y", "request_time": 0.25}}'
    )
    assert a is not None
    assert b is not None
    assert (a.timestamp, a.status, a.path, a.latency_ms) == (TEN_AM, 500, "/x", 12.5)
    assert (b.timestamp, b.status, b.latency_ms) == (TEN_AM, 201, 250.0)
    assert parser.parse('{"status": 200}') is None  # no timestamp
    assert parser.parse("[1, 2]") is None
    assert parser.parse("{not json") is None


def test_auto_parser_and_registry() -> None:
    auto = AutoParser()
    assert auto.parse('{"time": 0, "status": 200}') is not None
    assert auto.parse('1.2.3.4 - - [19/Oct/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 1') is not None

    @register_parser("constant-test")
    class ConstantParser:
        def parse(self, line: str) -> LogRecord | None:
            return LogRecord(timestamp=0.0, ip="-", method="-", path=line, status=200)

    assert "constant-test" in available_parsers()
    rec = get_parser("constant-test").parse("/hello")
    assert rec is not None
    assert rec.path == "/hello"
    with pytest.raises(ValueError, match="unknown parser"):
        get_parser("nope")
