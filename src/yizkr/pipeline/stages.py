"""Content hashes and atomic writes for the pipeline's files.

The stages before the review gates write plain-file views of their
outputs into each document's directory, and `derivations.json` records
what every derived file was made from by content hash
(yizkr.pipeline.orchestrate). These helpers give those files, the run
directory (yizkr.pipeline.rundir) and the LLM cache
(yizkr.pipeline.llmcache) one canonical serialisation to hash, and
writes that readers see whole or not at all.

Nothing here reads modification times: a file is current while its
content hashes the same.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """The one serialisation every hash is taken over."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha1_file(path: Path) -> str:
    """The media SHA-1: a document's content identity (ADR-0010)."""
    return _file_digest(path, "sha1")


def sha256_file(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or None when it does not exist."""
    if not path.is_file():
        return None
    return _file_digest(path, "sha256")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def strip_volatile(value: Any, volatile: Iterable[str]) -> Any:
    """A copy of `value` without the volatile fields.

    Each entry is a dotted path into nested objects ("extraction_run.
    started_at"); a list on the way fans the rest of the path out over
    its elements."""
    copy = _json_copy(value)
    for path in volatile:
        _drop(copy, path.split("."))
    return copy


def _drop(node: Any, parts: list[str]) -> None:
    if isinstance(node, list):
        for element in node:
            _drop(element, parts)
        return
    if not isinstance(node, dict) or not parts or parts[0] not in node:
        return
    if len(parts) == 1:
        del node[parts[0]]
    else:
        _drop(node[parts[0]], parts[1:])


def content_hash(value: Any, *, volatile: Iterable[str] = ()) -> str:
    """Canonical content hash of an output, with its volatile fields (run
    timestamps, engine build strings) left out, so an identical recompute
    hashes the same."""
    return sha256_text(canonical_json(strip_volatile(value, volatile)))


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


def read_jsonl(path: Path) -> list[dict]:
    """Entries of an append-only log; a torn line (its writer died
    mid-write) and a blank line are skipped.

    Records are split at newlines only: a record's strings may hold other
    line separators (U+2028, U+2029, U+0085), which the writers leave raw.
    Each line is decoded on its own, so a line torn inside a multi-byte
    character is skipped like any other torn line. A line nested too
    deeply for the decoder to recurse through is skipped the same way: no
    writer here produces one, but a reader does not stop on one either.
    """
    if not path.is_file():
        return []
    entries = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError included
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries
