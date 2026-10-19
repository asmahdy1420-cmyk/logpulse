from __future__ import annotations

import io
import json
import random
import subprocess
import sys
from pathlib import Path

from logpulse.anomaly import AnomalyDetector
from logpulse.cli import main, parse_duration
from logpulse.parsers import get_parser
from logpulse.pipeline import Pipeline
from logpulse.sinks import JsonLinesSink
from logpulse.sources import read_lines
from logpulse.store import MetricsStore
from logpulse.windows import Windower

ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_over_fixture(sample_log: Path) -> None:
    store = MetricsStore()
    pipeline = Pipeline(get_parser("auto"), Windower(60), [store])
    stats = pipeline.run(read_lines(str(sample_log)))
    assert (stats.lines, stats.parsed, stats.parse_errors, stats.windows) == (9, 8, 1, 2)
    first, second = store.history()
    assert first.requests == 5
    assert first.status_codes == {"200": 4, "502": 1}
    assert first.top_paths[0] == ("/api/products", 2)
    assert second.requests == 3
    assert second.error_rate == 1 / 3


def test_synthetic_incident_is_detected(tmp_path: Path) -> None:
    log = tmp_path / "gen.log"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_logs.py"),
            "--duration",
            "900",
            "--rate",
            "20",
            "--start",
            "1792399980",
            "--incident-at",
            "600",
            "--seed",
            "5",
            "-o",
            str(log),
        ],
        check=True,
    )
    buf = io.StringIO()
    pipeline = Pipeline(
        get_parser("nginx"), Windower(60), [JsonLinesSink(buf)], AnomalyDetector(warmup=5)
    )
    stats = pipeline.run(read_lines(str(log)))
    assert stats.parse_errors == 0
    records = [json.loads(line) for line in buf.getvalue().splitlines()]
    alerts = [r for r in records if r["type"] == "alert"]
    assert {a["metric"] for a in alerts} == {"error_rate", "p95_latency_ms"}
    # Start is minute-aligned, so the incident (600-660 s) fills exactly one window.
    assert {a["window_start"] - 1792399980 for a in alerts} <= {600.0, 660.0}


def test_follow_mode_closes_windows_by_wall_clock() -> None:
    now = [1000.0]
    store = MetricsStore()
    pipeline = Pipeline(get_parser("json"), Windower(10), [store], clock=lambda: now[0])
    pipeline.process_line('{"ts": 1001, "status": 200, "path": "/"}')
    assert store.latest() is None
    now[0] = 1011.0
    pipeline.tick()  # idle heartbeat from the follow source
    latest = store.latest()
    assert latest is not None
    assert latest.requests == 1


def test_cli_end_to_end(sample_log: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "out.jsonl"
    code = main(
        [
            "run",
            "--source",
            str(sample_log),
            "--window",
            "1m",
            "--sink",
            f"jsonl:{out}",
            "--sink",
            "table",
        ]
    )
    assert code == 0
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["requests"] for line in lines] == [5, 3]
    captured = capsys.readouterr()
    assert "requests 5" in captured.out
    assert "1 unparsable" in captured.err


def test_parse_duration() -> None:
    assert parse_duration("60s") == 60.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("250ms") == 0.25
    assert parse_duration("1h") == 3600.0
    assert parse_duration("90") == 90.0


def test_random_order_within_lateness_matches_sorted() -> None:
    rng = random.Random(9)
    timestamps = [i * 0.1 for i in range(3000)]
    shuffled = timestamps[:]
    for i in range(len(shuffled) - 1):  # local disorder of at most a couple of seconds
        if rng.random() < 0.3:
            j = min(len(shuffled) - 1, i + rng.randint(1, 20))
            shuffled[i], shuffled[j] = shuffled[j], shuffled[i]

    def run(ts_list: list[float]) -> list[int]:
        store = MetricsStore()
        p = Pipeline(get_parser("json"), Windower(30, allowed_lateness=5), [store])
        p.run(json.dumps({"ts": 1_000_000 + t, "status": 200}) for t in ts_list)
        assert p.stats.late_dropped == 0
        return [w.requests for w in store.history(100)]

    assert run(shuffled) == run(timestamps)
