"""Count-Min Sketch and a heavy-hitters tracker built on top of it.

Count-Min Sketch (Cormode and Muthukrishnan, 2005) keeps ``depth`` rows of ``width``
counters. Every item increments one counter per row; its estimate is the minimum over
those counters. Estimates never undercount, and with ``width = ceil(e / epsilon)`` and
``depth = ceil(ln(1 / delta))`` the overcount is at most ``epsilon * N`` with probability
``1 - delta`` (N = total count). Updates and queries are O(depth); memory is
O(width * depth), independent of the number of distinct keys.

Row indexes use double hashing (Kirsch and Mitzenmacher): one keyed BLAKE2b digest yields
two 64-bit values h1 and h2, and row i uses ``(h1 + i * h2) mod width``.
"""

from __future__ import annotations

import hashlib
import heapq
import math


class CountMinSketch:
    __slots__ = ("_key", "_tables", "depth", "total", "width")

    def __init__(self, width: int = 1024, depth: int = 4, seed: int = 0) -> None:
        if width < 1 or depth < 1:
            raise ValueError("width and depth must be positive")
        self.width = width
        self.depth = depth
        self.total = 0
        self._key = seed.to_bytes(8, "little", signed=False)
        self._tables: list[list[int]] = [[0] * width for _ in range(depth)]

    @classmethod
    def from_error(cls, epsilon: float, delta: float, seed: int = 0) -> CountMinSketch:
        """Size a sketch so that overcount <= epsilon * N with probability 1 - delta."""
        if not (0 < epsilon < 1 and 0 < delta < 1):
            raise ValueError("epsilon and delta must be within (0, 1)")
        width = math.ceil(math.e / epsilon)
        depth = math.ceil(math.log(1.0 / delta))
        return cls(width=width, depth=depth, seed=seed)

    @property
    def epsilon(self) -> float:
        return math.e / self.width

    def _indexes(self, item: str) -> list[int]:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16, key=self._key).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:], "little") | 1
        w = self.width
        return [(h1 + i * h2) % w for i in range(self.depth)]

    def add(self, item: str, count: int = 1) -> int:
        """Add ``count`` occurrences of ``item`` and return its new estimate."""
        if count < 0:
            raise ValueError("count must be non-negative")
        self.total += count
        estimate = None
        for row, idx in zip(self._tables, self._indexes(item)):
            row[idx] += count
            value = row[idx]
            if estimate is None or value < estimate:
                estimate = value
        return estimate or 0

    def estimate(self, item: str) -> int:
        return min(row[idx] for row, idx in zip(self._tables, self._indexes(item)))

    def __getitem__(self, item: str) -> int:
        return self.estimate(item)

    def merge(self, other: CountMinSketch) -> CountMinSketch:
        if (self.width, self.depth, self._key) != (other.width, other.depth, other._key):
            raise ValueError("can only merge sketches with identical shape and seed")
        for mine, theirs in zip(self._tables, other._tables):
            for i, value in enumerate(theirs):
                if value:
                    mine[i] += value
        self.total += other.total
        return self


class HeavyHitters:
    """Approximate top-k items: a Count-Min Sketch plus a bounded candidate set.

    The candidate set holds at most ``k`` items with their latest estimates. A min-heap
    with lazy invalidation gives the smallest candidate in O(log k) amortised, so each
    update costs O(depth + log k) and memory is O(width * depth + k).
    """

    __slots__ = ("_heap", "_top", "k", "sketch")

    def __init__(self, k: int = 10, width: int = 1024, depth: int = 4, seed: int = 0) -> None:
        if k < 1:
            raise ValueError("k must be positive")
        self.k = k
        self.sketch = CountMinSketch(width=width, depth=depth, seed=seed)
        self._top: dict[str, int] = {}
        self._heap: list[tuple[int, str]] = []

    def _min_candidate(self) -> tuple[int, str]:
        heap = self._heap
        while heap:
            count, item = heap[0]
            if self._top.get(item) == count:
                return count, item
            heapq.heappop(heap)  # stale entry
        raise LookupError("no candidates")

    def _set(self, item: str, estimate: int) -> None:
        self._top[item] = estimate
        heapq.heappush(self._heap, (estimate, item))
        if len(self._heap) > 8 * self.k + 64:
            self._heap = [(c, i) for i, c in self._top.items()]
            heapq.heapify(self._heap)

    def add(self, item: str, count: int = 1) -> None:
        estimate = self.sketch.add(item, count)
        if item in self._top or len(self._top) < self.k:
            self._set(item, estimate)
            return
        min_count, min_item = self._min_candidate()
        if estimate > min_count:
            del self._top[min_item]
            heapq.heappop(self._heap)
            self._set(item, estimate)

    def top(self, n: int | None = None) -> list[tuple[str, int]]:
        ranked = sorted(self._top.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[: n or self.k]

    def merge(self, other: HeavyHitters) -> HeavyHitters:
        self.sketch.merge(other.sketch)
        candidates = set(self._top) | set(other._top)
        scored = sorted(
            ((self.sketch.estimate(item), item) for item in candidates),
            key=lambda pair: (-pair[0], pair[1]),
        )[: self.k]
        self._top = {item: count for count, item in scored}
        self._heap = [(count, item) for count, item in scored]
        heapq.heapify(self._heap)
        return self
