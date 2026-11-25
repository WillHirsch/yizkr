"""Durable and atomic writes of text files.

Document views, run records, caches and the instance records are written
so that a reader sees a file whole or not at all: `atomic_write_text`
writes the new text under a staging name beside the target, flushes it to
the disk and replaces the target with it in one step. The helpers import
nothing from yizkr, so core and the pipeline share them.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def write_synced(path: Path, text: str) -> None:
    """Write `text` to `path` and flush it to disk before returning."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def staging_path(path: Path) -> Path:
    """A fresh name beside `path` to write it under first: dot-prefixed,
    so nothing that reads the directory's entries takes it for one."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step: readers see the old file or
    the new one, never a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(path)
    try:
        write_synced(staging, text)
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
