"""A merging t-digest for streaming quantile estimation.

The digest keeps a sorted list of centroids (mean, weight). New points are buffered and
periodically merged into the centroid list in one sorted pass. During that pass adjacent
centroids are combined while the k1 scale function

    k(q) = delta / (2 * pi) * asin(2q - 1)

grows by at most 1 across the merged centroid. Because k is steep near q = 0 and q = 1,
centroids at the tails stay tiny and the extreme quantiles (p95, p99) stay accurate, while
centroids around the median are allowed to grow large.

Memory is O(delta) centroids, insertion is amortised O(log n) per point (one sort per
buffer flush), and a quantile query is O(delta). Digests are mergeable, which is what lets
sliding windows combine per-pane digests.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


class TDigest:
    __slots__ = (
        "_buffer",
        "_buffer_limit",
        "_count",
        "_max",
        "_means",
        "_min",
        "_weights",
        "compression",
    )

    def __init__(self, compression: float = 100.0, buffer_limit: int | None = None) -> None:
        if compression < 10:
            raise ValueError("compression must be >= 10")
        self.compression = float(compression)
        self._means: list[float] = []
        self._weights: list[float] = []
        self._buffer: list[tuple[float, float]] = []
        self._buffer_limit = buffer_limit or int(compression * 5)
        self._count = 0.0
        self._min = math.inf
        self._max = -math.inf

    # ------------------------------------------------------------------ ingestion
    def add(self, value: float, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        if math.isnan(value):
            raise ValueError("cannot add NaN")
        self._buffer.append((float(value), float(weight)))
        self._count += weight
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value
        if len(self._buffer) >= self._buffer_limit:
            self._compress()

    def extend(self, values: Iterable[float]) -> None:
        for v in values:
            self.add(v)

    def merge(self, other: TDigest) -> TDigest:
        """Fold ``other`` into this digest in place and return ``self``."""
        if other._count == 0:
            return self
        self._buffer.extend(zip(other._means, other._weights))
        self._buffer.extend(other._buffer)
        self._count += other._count
        self._min = min(self._min, other._min)
        self._max = max(self._max, other._max)
        self._compress()
        return self

    # ------------------------------------------------------------------ internals
    def _k(self, q: float) -> float:
        q = min(1.0, max(0.0, q))
        return self.compression / (2.0 * math.pi) * math.asin(2.0 * q - 1.0)

    def _compress(self) -> None:
        if not self._buffer:
            return
        items = sorted([*zip(self._means, self._weights), *self._buffer])
        self._buffer = []
        total = self._count
        means: list[float] = []
        weights: list[float] = []

        cur_mean, cur_weight = items[0]
        weight_before = 0.0
        k_lower = self._k(0.0)
        for mean, weight in items[1:]:
            q_upper = (weight_before + cur_weight + weight) / total
            if self._k(q_upper) - k_lower <= 1.0:
                cur_weight += weight
                cur_mean += (mean - cur_mean) * weight / cur_weight
            else:
                means.append(cur_mean)
                weights.append(cur_weight)
                weight_before += cur_weight
                k_lower = self._k(weight_before / total)
                cur_mean, cur_weight = mean, weight
        means.append(cur_mean)
        weights.append(cur_weight)
        self._means = means
        self._weights = weights

    # ------------------------------------------------------------------ queries
    @property
    def count(self) -> float:
        return self._count

    @property
    def centroid_count(self) -> int:
        self._compress()
        return len(self._means)

    def __len__(self) -> int:
        return int(self._count)

    def quantile(self, q: float) -> float:
        """Estimate the value at quantile ``q`` (0..1). Returns NaN when empty."""
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be within [0, 1]")
        self._compress()
        if self._count == 0:
            return math.nan
        if q == 0.0:
            return self._min
        if q == 1.0:
            return self._max
        means, weights = self._means, self._weights
        if len(means) == 1:
            return means[0]

        target = q * self._count
        # Left tail: interpolate between the observed minimum and the first centroid centre.
        first_half = weights[0] / 2.0
        if target < first_half:
            return self._min + (means[0] - self._min) * (target / first_half)

        cumulative = 0.0
        for i in range(len(means) - 1):
            mid = cumulative + weights[i] / 2.0
            next_mid = cumulative + weights[i] + weights[i + 1] / 2.0
            if target <= next_mid:
                frac = (target - mid) / (next_mid - mid)
                return means[i] + frac * (means[i + 1] - means[i])
            cumulative += weights[i]

        # Right tail: interpolate between the last centroid centre and the observed maximum.
        last_half = weights[-1] / 2.0
        frac = (target - (self._count - last_half)) / last_half
        return min(self._max, means[-1] + frac * (self._max - means[-1]))

    def quantiles(self, qs: Iterable[float]) -> list[float]:
        return [self.quantile(q) for q in qs]
