"""A non-blocking process lock so two collection runs (e.g. a scheduled run and a manual
one) can never overlap on the same machine."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockHeld(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockHeld("Another collection run is in progress.") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
