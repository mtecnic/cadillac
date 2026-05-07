"""Atomic file I/O + cross-process locking helpers.

Used by long-lived state files that get hit from multiple threads or sub-
processes during a build (scratch, memory, progress, phase history). The
existing call sites all do `open(path, "w")` or `open(path, "a")` directly,
which has two real failure modes the audit caught:

  1. CONCURRENT APPEND CORRUPTION: two threads/procs `open(path, "a")` and
     `f.write(...)` can interleave at the OS level. With small writes (the
     usual case) you usually get atomic-line behavior on Linux, but with
     anything larger (e.g., a full Lesson JSON object), you can get partial
     lines that subsequent loaders silently skip — losing data.

  2. TRUNCATE-THEN-WRITE: `open(path, "w")` truncates immediately. If the
     process is killed between truncate and the final flush, the file is
     empty. This bit save_all() in memory.py — a crash mid-rewrite wiped
     every lesson the build had ever recorded.

Both are fixed with the patterns here:

  - `with file_lock(path)`: cross-process advisory lock via fcntl, gates
    both readers and writers. Held only as long as the with-block.
  - `atomic_write_text(path, text)`: write to a sibling `path.tmp.<pid>.<ts>`
    then `os.replace()` it onto path. POSIX rename is atomic; readers see
    either the old file or the new file, never a half-written one.
  - `atomic_append_lines(path, lines)`: lock + append + fsync, so concurrent
    writers serialize through the lock instead of racing at the kernel level.
"""

from __future__ import annotations

import errno
import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Iterable, Iterator


def _lock_path(target_path: str) -> str:
    """Sidecar lock file. We don't lock the target itself because some
    consumers want to open it read-only without acquiring the lock."""
    return target_path + ".lock"


@contextmanager
def file_lock(path: str, timeout: float = 30.0) -> Iterator[None]:
    """Hold an advisory exclusive lock for the duration of the with-block.

    Cross-process safe (fcntl.flock). Inter-thread safe within the same
    process because flock on the same fd from a second thread blocks until
    the first releases.

    Raises TimeoutError if the lock can't be obtained within `timeout`
    seconds — better than blocking the build forever on a stuck lock from
    a previous crashed run.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lock_path = _lock_path(path)
    # O_RDWR | O_CREAT — file just needs to exist; we never read or write
    # actual data through it.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"could not acquire lock on {path} within {timeout}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def atomic_write_text(path: str, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically via temp file + rename.

    POSIX guarantees `os.replace()` is atomic on the same filesystem, so a
    reader either sees the old file or the new one, never a half-written
    state. The temp file lives in the target's directory so the rename
    stays within one filesystem.
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Don't leave temp files behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_append_lines(path: str, lines: Iterable[str], *,
                         encoding: str = "utf-8") -> None:
    """Append `lines` (each newline-terminated by us) to `path` under a
    cross-process lock. fsync after the write so we don't lose entries on
    a power cut.

    Use this for JSONL-style accumulating files (memory.jsonl, phase
    history). Readers don't need the lock — partial writes are impossible
    because we hold the lock through fsync.
    """
    payload = "".join(line.rstrip("\n") + "\n" for line in lines)
    if not payload:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with file_lock(path):
        with open(path, "a", encoding=encoding) as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
