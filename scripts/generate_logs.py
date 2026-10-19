#!/usr/bin/env python3
"""Generate realistic nginx-combined or JSON access logs, with an optional incident.

Examples:
    # 10 minutes of traffic at ~50 req/s, with a 60 s incident starting at minute 6
    python scripts/generate_logs.py --duration 600 --rate 50 --incident-at 360 > access.log

    # JSON lines, streamed in real time (pair with: logpulse run --source app.log --follow)
    python scripts/generate_logs.py --format json --realtime --rate 20 -o app.log

During an incident the 5xx share jumps to ~25% and latency is multiplied by 6.
Standard library only, so it runs anywhere Python does.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone

PATHS = [
    "/",
    "/api/products",
    "/api/products/{id}",
    "/api/cart",
    "/api/checkout",
    "/api/search",
    "/api/users/me",
    "/login",
    "/static/app.js",
    "/static/app.css",
    "/health",
    "/api/orders",
]
METHODS = {"/api/cart": "POST", "/api/checkout": "POST", "/login": "POST"}
AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14) Chrome/128.0 Mobile Safari/537.36",
    "curl/8.7.1",
]


def zipf_choice(rng: random.Random, items: list[str], s: float = 1.1) -> str:
    weights = [1.0 / (i + 1) ** s for i in range(len(items))]
    return rng.choices(items, weights=weights, k=1)[0]


class TrafficModel:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        # A few busy clients and a long tail, like real traffic.
        self.ips = [f"10.0.{i // 256}.{i % 256}" for i in range(1, 500)]
        self.rng.shuffle(self.ips)

    def request(self, incident: bool) -> dict:
        rng = self.rng
        path = zipf_choice(rng, PATHS).replace("{id}", str(rng.randint(1, 5000)))
        roll = rng.random()
        if incident and roll < 0.25:
            status = rng.choice([500, 502, 503, 504])
        elif roll < 0.005:
            status = 500
        elif roll < 0.04:
            status = rng.choice([400, 401, 403, 404, 404, 404, 429])
        elif roll < 0.06:
            status = rng.choice([301, 302, 304])
        else:
            status = 200
        latency = rng.lognormvariate(math.log(35), 0.6)  # median ~35 ms
        if incident:
            latency *= 6
        return {
            "ip": zipf_choice(rng, self.ips, s=0.9),
            "method": METHODS.get(path.split("?")[0], "GET"),
            "path": path,
            "status": status,
            "bytes": 0 if status == 304 else int(rng.lognormvariate(math.log(2500), 1.0)),
            "latency_ms": round(latency, 3),
            "user_agent": rng.choice(AGENTS),
        }


def format_line(fmt: str, ts: float, req: dict) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if fmt == "json":
        return json.dumps(
            {
                "timestamp": dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "level": "info",
                "msg": "request",
                **req,
            }
        )
    stamp = dt.strftime("%d/%b/%Y:%H:%M:%S +0000")
    return (
        f'{req["ip"]} - - [{stamp}] "{req["method"]} {req["path"]} HTTP/1.1" '
        f'{req["status"]} {req["bytes"]} "-" "{req["user_agent"]}" '
        f"{req['latency_ms'] / 1000:.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--format", choices=("nginx", "json"), default="nginx")
    ap.add_argument("--rate", type=float, default=50.0, help="mean requests per second")
    ap.add_argument("--duration", type=float, default=300.0, help="seconds of traffic")
    ap.add_argument(
        "--start",
        type=float,
        default=None,
        help="start time as epoch seconds (default: now - duration)",
    )
    ap.add_argument(
        "--incident-at",
        type=float,
        default=None,
        help="seconds after start when an incident begins",
    )
    ap.add_argument("--incident-duration", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--realtime",
        action="store_true",
        help="emit lines at wall-clock pace with current timestamps",
    )
    ap.add_argument("-o", "--output", default="-", help="output file (default stdout)")
    args = ap.parse_args(argv)

    model = TrafficModel(args.seed)
    out = sys.stdout if args.output == "-" else open(args.output, "a", encoding="utf-8")  # noqa: SIM115
    start = time.time() if args.realtime else (args.start or time.time() - args.duration)
    t = start
    try:
        while t - start < args.duration:
            t += model.rng.expovariate(args.rate)  # Poisson arrivals
            if args.realtime:
                time.sleep(max(0.0, t - time.time()))
            offset = t - start
            incident = (
                args.incident_at is not None
                and args.incident_at <= offset < args.incident_at + args.incident_duration
            )
            out.write(format_line(args.format, t, model.request(incident)) + "\n")
            if args.realtime:
                out.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
