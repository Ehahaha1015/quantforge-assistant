"""Host-owned local artifact paths and crash-released single-writer lock."""

from __future__ import annotations

import base64
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from quantforge.experiments import canonical_json


def local_root(value: str | Path) -> Path:
    path = Path(value).absolute()
    if str(value).startswith(("\\\\", "//")) or "://" in str(value):
        raise ValueError("local artifact root required")
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("redirected local path")
    if not path.is_dir():
        raise ValueError("existing local root required")
    return path.resolve(strict=True)


def run_directory(root: Path, run_id: str) -> Path:
    encoded = (
        base64.b32encode(run_id.encode("ascii")).decode("ascii").rstrip("=").lower()
    )
    if not encoded or len(encoded) > 205:
        raise ValueError("invalid run identifier")
    return root / ("run-" + encoded)


def write_exclusive(path: Path, value: object) -> None:
    with path.open("xb") as stream:
        stream.write(canonical_json(value).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    lock = root / ".writer.lock"
    if lock.exists() and lock.is_symlink():
        raise ValueError("redirected writer lock")
    with lock.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
