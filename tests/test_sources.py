from __future__ import annotations

import os
import threading
from pathlib import Path

from logpulse.sources import read_lines


def test_read_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("one\ntwo\nthree\n")
    assert list(read_lines(str(path))) == ["one", "two", "three"]


def test_follow_picks_up_appends_partial_lines_and_rotation(tmp_path: Path) -> None:
    path = tmp_path / "live.log"
    path.write_text("first\n")
    stop = threading.Event()
    seen: list[str] = []
    gen = read_lines(str(path), follow=True, poll_interval=0.01, stop=stop)

    def next_line() -> str:
        for item in gen:
            if item is not None:
                return item
        raise AssertionError("generator ended")

    seen.append(next_line())
    with path.open("a") as fh:
        fh.write("sec")
        fh.flush()
        fh.write("ond\n")
    seen.append(next_line())

    os.rename(path, tmp_path / "live.log.1")  # logrotate-style rotation
    path.write_text("after-rotation\n")
    seen.append(next_line())
    stop.set()
    gen.close()
    assert seen == ["first", "second", "after-rotation"]
