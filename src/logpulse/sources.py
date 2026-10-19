"""Line sources: whole files, stdin, and ``tail -F`` style follow mode."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from typing import IO


def read_lines(
    source: str,
    follow: bool = False,
    poll_interval: float = 0.25,
    stop: threading.Event | None = None,
    from_end: bool = False,
) -> Iterator[str | None]:
    """Yield lines from ``source`` (a path, or ``-`` for stdin).

    In follow mode the generator never ends on its own: at end of file it yields ``None``
    as an idle heartbeat (so callers can close windows by wall clock), then polls again.
    Truncation and rotation (inode change) are detected and the file is reopened.
    """
    if source == "-":
        yield from _read_stream(sys.stdin)
        return
    if not follow:
        with open(source, encoding="utf-8", errors="replace") as fh:
            yield from _read_stream(fh)
        return
    yield from _follow(source, poll_interval, stop or threading.Event(), from_end)


def _read_stream(stream: IO[str]) -> Iterator[str]:
    for line in stream:
        yield line.rstrip("\n")


def _follow(
    path: str, poll_interval: float, stop: threading.Event, from_end: bool
) -> Iterator[str | None]:
    fh = None
    inode = None
    partial = ""
    try:
        while not stop.is_set():
            if fh is None:
                try:
                    fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
                except FileNotFoundError:
                    yield None
                    stop.wait(poll_interval)
                    continue
                inode = os.fstat(fh.fileno()).st_ino
                if from_end:
                    fh.seek(0, os.SEEK_END)
                    from_end = False

            chunk = fh.readline()
            if chunk:
                if chunk.endswith("\n"):
                    yield partial + chunk[:-1]
                    partial = ""
                else:
                    partial += chunk  # writer has not finished the line yet
                continue

            # End of file: check for rotation or truncation, then idle.
            try:
                st = os.stat(path)
                if st.st_ino != inode or st.st_size < fh.tell():
                    fh.close()
                    fh, partial = None, ""
                    continue
            except FileNotFoundError:
                pass
            yield None
            stop.wait(poll_interval)
    finally:
        if fh is not None:
            fh.close()


def wall_clock() -> float:
    return time.time()
