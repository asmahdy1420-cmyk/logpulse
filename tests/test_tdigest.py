from __future__ import annotations

import math
import random

import pytest

from logpulse.sketches import TDigest


def exact_quantile(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


@pytest.mark.parametrize("q", [0.5, 0.9, 0.95, 0.99, 0.999])
def test_uniform_quantiles_within_tolerance(q: float) -> None:
    rng = random.Random(7)
    values = [rng.random() for _ in range(50_000)]
    digest = TDigest(compression=100)
    digest.extend(values)
    assert digest.quantile(q) == pytest.approx(exact_quantile(sorted(values), q), abs=0.005)


def test_lognormal_latency_tail_relative_error() -> None:
    rng = random.Random(11)
    values = [rng.lognormvariate(math.log(40), 0.8) for _ in range(50_000)]
    digest = TDigest(compression=100)
    digest.extend(values)
    ordered = sorted(values)
    for q in (0.5, 0.95, 0.99):
        assert digest.quantile(q) == pytest.approx(exact_quantile(ordered, q), rel=0.02)


def test_memory_stays_bounded() -> None:
    digest = TDigest(compression=100)
    digest.extend(float(i) for i in range(200_000))
    assert digest.count == 200_000
    assert digest.centroid_count < 200  # O(compression), not O(n)


def test_merge_matches_single_digest() -> None:
    rng = random.Random(3)
    values = [rng.gauss(100, 15) for _ in range(40_000)]
    whole = TDigest()
    whole.extend(values)
    parts = [TDigest() for _ in range(4)]
    for i, v in enumerate(values):
        parts[i % 4].add(v)
    merged = TDigest()
    for part in parts:
        merged.merge(part)
    assert merged.count == whole.count
    for q in (0.5, 0.95, 0.99):
        assert merged.quantile(q) == pytest.approx(whole.quantile(q), rel=0.01)


def test_edge_cases() -> None:
    digest = TDigest()
    assert math.isnan(digest.quantile(0.5))
    digest.add(42.0)
    assert digest.quantile(0.01) == 42.0
    assert digest.quantile(0.99) == 42.0
    digest.add(1.0)
    digest.add(100.0)
    assert digest.quantile(0.0) == 1.0
    assert digest.quantile(1.0) == 100.0
    with pytest.raises(ValueError, match="NaN"):
        digest.add(float("nan"))
    with pytest.raises(ValueError, match="q must be"):
        digest.quantile(1.5)
