from __future__ import annotations

import random
from collections import Counter

import pytest

from logpulse.sketches import CountMinSketch, HeavyHitters


def zipf_stream(n: int, distinct: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    keys = [f"/path/{i}" for i in range(distinct)]
    weights = [1.0 / (i + 1) for i in range(distinct)]
    return rng.choices(keys, weights=weights, k=n)


def test_never_underestimates_and_respects_error_bound() -> None:
    epsilon, delta = 0.001, 0.01
    cms = CountMinSketch.from_error(epsilon, delta)
    assert cms.width == 2719
    assert cms.depth == 5
    stream = zipf_stream(100_000, 5_000, seed=1)
    for item in stream:
        cms.add(item)
    truth = Counter(stream)
    bound = epsilon * cms.total
    violations = 0
    for item, count in truth.items():
        estimate = cms.estimate(item)
        assert estimate >= count
        if estimate - count > bound:
            violations += 1
    # Guarantee is per query with probability 1 - delta.
    assert violations <= delta * len(truth)


def test_unseen_item_estimate_is_small() -> None:
    cms = CountMinSketch(width=2048, depth=5)
    for item in zipf_stream(20_000, 1_000, seed=2):
        cms.add(item)
    assert cms.estimate("never-seen") <= cms.epsilon * cms.total


def test_merge_equals_combined_stream() -> None:
    a, b, both = CountMinSketch(512, 4), CountMinSketch(512, 4), CountMinSketch(512, 4)
    for i, item in enumerate(zipf_stream(10_000, 300, seed=3)):
        (a if i % 2 else b).add(item)
        both.add(item)
    a.merge(b)
    assert a.total == both.total
    assert all(a.estimate(f"/path/{i}") == both.estimate(f"/path/{i}") for i in range(300))
    with pytest.raises(ValueError, match="identical shape"):
        a.merge(CountMinSketch(256, 4))


def test_heavy_hitters_find_true_top_k() -> None:
    stream = zipf_stream(50_000, 2_000, seed=4)
    hh = HeavyHitters(k=5, width=2048, depth=5)
    for item in stream:
        hh.add(item)
    expected = [item for item, _ in Counter(stream).most_common(5)]
    found = [item for item, _ in hh.top()]
    assert found == expected


def test_heavy_hitters_merge() -> None:
    left, right = HeavyHitters(k=3), HeavyHitters(k=3)
    for _ in range(50):
        left.add("/a")
    for _ in range(40):
        right.add("/b")
    for _ in range(30):
        left.add("/c")
        right.add("/c")
    for i in range(20):
        left.add(f"/noise/{i}")
    left.merge(right)
    assert [item for item, _ in left.top()] == ["/c", "/a", "/b"]
