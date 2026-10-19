"""Pluggable log parsers.

A parser is any object with ``parse(line: str) -> LogRecord | None``. Parsers return
``None`` for lines they cannot understand; the pipeline counts those instead of failing.
New formats are added with the :func:`register_parser` decorator.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from logpulse.models import LogRecord


class Parser(Protocol):
    name: str

    def parse(self, line: str) -> LogRecord | None: ...


_REGISTRY: dict[str, Callable[[], Parser]] = {}


def register_parser(name: str) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_parser(name: str) -> Parser:
    try:
        return _REGISTRY[name]()
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown parser {name!r} (available: {known})") from None


def available_parsers() -> list[str]:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- nginx
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}

_COMBINED = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r"(?P<status>\d{3}) (?P<bytes>\d+|-)"
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?'
    r"(?:\s+(?P<rt>\d+(?:\.\d+)?))?"
)


def parse_clf_time(value: str) -> float:
    """Parse ``10/Oct/2026:13:55:36 +0000`` without relying on the process locale."""
    # dd/Mon/yyyy:HH:MM:SS +zzzz
    day, month, rest = value[0:2], value[3:6], value[7:]
    year, hh, mm, ss = rest[0:4], rest[5:7], rest[8:10], rest[11:13]
    tz = rest[14:19]
    sign = -1 if tz[0] == "-" else 1
    offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    dt = datetime(
        int(year), _MONTHS[month], int(day), int(hh), int(mm), int(ss), tzinfo=timezone(offset)
    )
    return dt.timestamp()


def normalise_path(target: str) -> str:
    path = target.split("?", 1)[0].split("#", 1)[0]
    return path or "/"


@register_parser("nginx")
class NginxParser:
    """nginx/Apache *combined* format, optionally followed by ``$request_time`` (seconds).

    Example ``log_format``:  ``'$remote_addr - $remote_user [$time_local] "$request" '
    '$status $body_bytes_sent "$http_referer" "$http_user_agent" $request_time'``
    """

    name = "nginx"

    def parse(self, line: str) -> LogRecord | None:
        match = _COMBINED.match(line.strip())
        if match is None:
            return None
        try:
            ts = parse_clf_time(match["time"])
        except (KeyError, ValueError, IndexError):
            return None
        parts = match["request"].split()
        method, path = (parts[0], normalise_path(parts[1])) if len(parts) >= 2 else ("-", "-")
        size = match["bytes"]
        rt = match["rt"]
        return LogRecord(
            timestamp=ts,
            ip=match["ip"],
            method=method,
            path=path,
            status=int(match["status"]),
            bytes_sent=0 if size == "-" else int(size),
            latency_ms=float(rt) * 1000.0 if rt is not None else None,
            user_agent=match["agent"] or "",
        )


# --------------------------------------------------------------------------- JSON
def parse_timestamp(value: Any) -> float:
    """Accept epoch seconds/milliseconds or ISO-8601 strings (``Z`` suffix allowed)."""
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unsupported timestamp: {value!r}")


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


@register_parser("json")
class JsonParser:
    """One JSON object per line, with common field-name aliases.

    Latency is read from ``latency_ms``/``duration_ms`` (milliseconds) or
    ``request_time``/``duration`` (seconds).
    """

    name = "json"
    TIME_KEYS = ("timestamp", "time", "ts", "@timestamp")
    STATUS_KEYS = ("status", "status_code", "http_status")
    PATH_KEYS = ("path", "request_path", "url", "uri")
    IP_KEYS = ("ip", "client_ip", "remote_addr", "remote_ip")
    METHOD_KEYS = ("method", "http_method", "verb")
    BYTES_KEYS = ("bytes", "bytes_sent", "response_size")
    MS_KEYS = ("latency_ms", "duration_ms", "response_time_ms")
    SECONDS_KEYS = ("request_time", "duration", "latency")

    def parse(self, line: str) -> LogRecord | None:
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        raw_ts = _first(data, self.TIME_KEYS)
        raw_status = _first(data, self.STATUS_KEYS)
        if raw_ts is None or raw_status is None:
            return None
        try:
            ts = parse_timestamp(raw_ts)
            status = int(raw_status)
            latency = _first(data, self.MS_KEYS)
            if latency is not None:
                latency = float(latency)
            else:
                seconds = _first(data, self.SECONDS_KEYS)
                latency = float(seconds) * 1000.0 if seconds is not None else None
            size = int(_first(data, self.BYTES_KEYS) or 0)
        except (TypeError, ValueError):
            return None
        return LogRecord(
            timestamp=ts,
            ip=str(_first(data, self.IP_KEYS) or "-"),
            method=str(_first(data, self.METHOD_KEYS) or "-"),
            path=normalise_path(str(_first(data, self.PATH_KEYS) or "-")),
            status=status,
            bytes_sent=size,
            latency_ms=latency,
            user_agent=str(data.get("user_agent") or ""),
        )


@register_parser("auto")
class AutoParser:
    """Sniff each line: JSON objects go to :class:`JsonParser`, the rest to nginx."""

    name = "auto"

    def __init__(self) -> None:
        self._json = JsonParser()
        self._nginx = NginxParser()

    def parse(self, line: str) -> LogRecord | None:
        if line.lstrip().startswith("{"):
            return self._json.parse(line)
        return self._nginx.parse(line)
