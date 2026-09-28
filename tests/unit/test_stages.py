"""Offline tests for the pipeline's atomic writes (yizkr.pipeline.stages).

The unit tests skip fsync by default (tests/unit/conftest.py); the tests
here that check durability are marked `real_fsync` and wrap the real
calls to record what reaches them, and in what order.
"""

from __future__ import annotations

import os

import pytest

from tests.fixtures import example_universe as u
from yizkr.pipeline.stages import atomic_write_text, write_synced

SOURCES = {source.key: source for source in u.SOURCES}
# a transcription with non-ASCII text: its encoded size is not its length
POSTCARD = SOURCES["postcard-1938"].text
POSTCARD_BYTES = len(POSTCARD.encode("utf-8"))


def _file_id(stat: os.stat_result) -> tuple[int, int]:
    return stat.st_dev, stat.st_ino


@pytest.mark.real_fsync
def test_write_synced_returns_after_the_whole_text_is_fsynced(
    tmp_path, monkeypatch
):
    """The LLM cache links a file written this way into place, so the
    text must be on the disk when the call returns."""
    path = tmp_path / "entry.json"
    synced: list[tuple] = []
    real_fsync = os.fsync

    def fsync(fd):
        stat = os.fstat(fd)
        synced.append((_file_id(stat), stat.st_size))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    write_synced(path, POSTCARD)
    assert (_file_id(path.stat()), POSTCARD_BYTES) in synced
    assert path.read_text(encoding="utf-8") == POSTCARD


@pytest.mark.real_fsync
def test_the_staged_file_is_fsynced_whole_before_it_replaces_the_target(
    tmp_path, monkeypatch
):
    """A crash just after the rename must not leave the new name on a
    file whose bytes never reached the disk: the staged file is fsynced,
    with all of the text in it, before os.replace moves it over the
    target."""
    target = tmp_path / "transcription.txt"
    target.write_text("an older transcription", encoding="utf-8")
    calls: list[tuple] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        stat = os.fstat(fd)
        calls.append(("fsync", _file_id(stat), stat.st_size))
        return real_fsync(fd)

    def replace(src, dst, *args, **kwargs):
        calls.append(("replace", _file_id(os.stat(src)), os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    atomic_write_text(target, POSTCARD)

    replaced = [n for n, call in enumerate(calls) if call[0] == "replace"]
    assert len(replaced) == 1, calls
    _, staged, destination = calls[replaced[0]]
    assert destination == os.fspath(target)
    synced_sizes = [
        size
        for name, file_id, size in calls[: replaced[0]]
        if name == "fsync" and file_id == staged
    ]
    assert synced_sizes, f"the staged file was not fsynced first: {calls}"
    assert synced_sizes[-1] == POSTCARD_BYTES
    assert _file_id(target.stat()) == staged
    assert target.read_text(encoding="utf-8") == POSTCARD
