"""Command-line entry point: ``logpulse run --source access.log --window 60s``."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
from collections.abc import Sequence

from logpulse import __version__
from logpulse.anomaly import AnomalyDetector
from logpulse.parsers import available_parsers, get_parser
from logpulse.pipeline import Pipeline
from logpulse.sinks import JsonLinesSink, PostgresSink, Sink, TableSink
from logpulse.sources import read_lines, wall_clock
from logpulse.store import MetricsStore
from logpulse.windows import PaneStats, Windower

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(text: str) -> float:
    """``"60s"`` -> 60.0, ``"5m"`` -> 300.0, ``"250ms"`` -> 0.25, ``"90"`` -> 90.0."""
    match = _DURATION.match(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"invalid duration: {text!r} (try 30s, 5m, 1h)")
    seconds = float(match.group(1)) * _UNITS[match.group(2)]
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logpulse", description="Streaming log analytics for nginx and JSON logs."
    )
    parser.add_argument("--version", action="version", version=f"logpulse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process a log file or stdin")
    run.add_argument("--source", default="-", help="log file path, or - for stdin (default)")
    run.add_argument(
        "--format",
        default="auto",
        choices=available_parsers(),
        help="log format (default: auto-detect per line)",
    )
    run.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="keep reading as the file grows (tail -F); windows also close by "
        "wall clock when the file is idle",
    )
    run.add_argument(
        "--from-end", action="store_true", help="with --follow, start at the end of the file"
    )
    run.add_argument("--window", type=parse_duration, default=60.0, help="window size (60s)")
    run.add_argument(
        "--slide",
        type=parse_duration,
        default=None,
        help="slide for sliding windows (default: tumbling, slide = window)",
    )
    run.add_argument(
        "--lateness",
        type=parse_duration,
        default=None,
        help="allowed out-of-order lateness (default 0)",
    )
    run.add_argument("--top", type=int, default=5, help="top-N paths and IPs to report")
    run.add_argument(
        "--sink",
        action="append",
        default=None,
        metavar="SPEC",
        help="table | jsonl | jsonl:PATH | postgres (repeatable; default table)",
    )
    run.add_argument(
        "--pg-dsn",
        default=os.environ.get("LOGPULSE_PG_DSN"),
        help="PostgreSQL DSN for the postgres sink (env LOGPULSE_PG_DSN)",
    )
    run.add_argument("--pg-batch", type=int, default=50, help="rows per PostgreSQL batch")
    run.add_argument("--alpha", type=float, default=0.3, help="EWMA smoothing factor")
    run.add_argument("--z-threshold", type=float, default=3.0, help="alert z-score threshold")
    run.add_argument("--warmup", type=int, default=5, help="windows before alerting starts")
    run.add_argument(
        "--min-requests",
        type=int,
        default=20,
        help="skip anomaly scoring for windows with fewer requests",
    )
    run.add_argument(
        "--serve",
        action="store_true",
        help="expose current metrics over HTTP (needs the api extra)",
    )
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)

    sub.add_parser("parsers", help="list available log parsers")
    return parser


def build_sinks(args: argparse.Namespace, source_label: str) -> list[Sink]:
    sinks: list[Sink] = []
    for spec in args.sink or ["table"]:
        kind, _, target = spec.partition(":")
        if kind == "table":
            sinks.append(TableSink(top=args.top))
        elif kind == "jsonl":
            sinks.append(JsonLinesSink(target or "-"))
        elif kind == "postgres":
            if not args.pg_dsn:
                raise SystemExit("the postgres sink needs --pg-dsn or LOGPULSE_PG_DSN")
            sinks.append(
                PostgresSink(dsn=args.pg_dsn, source=source_label, batch_size=args.pg_batch)
            )
        elif kind != "none":
            raise SystemExit(f"unknown sink {spec!r}")
    return sinks


def build_pipeline(args: argparse.Namespace, store: MetricsStore | None = None) -> Pipeline:
    top = max(args.top, 1)
    windower = Windower(
        size=args.window,
        slide=args.slide,
        allowed_lateness=args.lateness or 0.0,
        stats_factory=lambda: PaneStats(top_k=top),
    )
    detector = AnomalyDetector(
        alpha=args.alpha,
        threshold=args.z_threshold,
        warmup=args.warmup,
        min_requests=args.min_requests,
    )
    source_label = "stdin" if args.source == "-" else os.path.basename(args.source)
    sinks = build_sinks(args, source_label)
    if store is not None:
        sinks.append(store)
    return Pipeline(
        get_parser(args.format),
        windower,
        sinks,
        detector,
        clock=wall_clock if args.follow else None,
    )


def cmd_run(args: argparse.Namespace) -> int:
    store = MetricsStore() if args.serve else None
    pipeline = build_pipeline(args, store)
    stop = threading.Event()
    lines = read_lines(args.source, follow=args.follow, stop=stop, from_end=args.from_end)

    if not args.serve:
        try:
            stats = pipeline.run(lines)
        except KeyboardInterrupt:
            pipeline.close()
            stats = pipeline.stats
        print(
            f"\nprocessed {stats.lines} lines: {stats.parsed} parsed, "
            f"{stats.parse_errors} unparsable, {stats.late_dropped} late, "
            f"{stats.windows} windows, {stats.alerts} alerts",
            file=sys.stderr,
        )
        return 0

    try:
        import uvicorn

        from logpulse.api import create_app
    except ImportError:
        raise SystemExit("--serve needs the api extra: pip install 'logpulse[api]'") from None
    assert store is not None
    store.stats_provider = pipeline.stats.as_dict
    worker = threading.Thread(target=pipeline.run, args=(lines,), daemon=True, name="pipeline")
    worker.start()
    try:
        uvicorn.run(create_app(store), host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "parsers":
        for name in available_parsers():
            print(name)
        return 0
    return cmd_run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
