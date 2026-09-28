"""Fixtures shared by the offline unit tests.

Two defaults keep the unit tests fast without changing what they check:

- **One BLAS thread.** Language detection (py3langid) runs on numpy,
  whose threaded OpenBLAS build starts a worker thread per CPU, and the
  workers spin after every call. `OPENBLAS_NUM_THREADS` defaults to 1
  here, before anything imports numpy; a value already set in the
  environment is kept.
- **No fsync.** `os.fsync`, `os.fdatasync` and `os.sync` are no-ops in
  every unit test (the `_no_fsync` fixture). The pipeline's atomic
  writes (yizkr.pipeline.stages) fsync every file they stage, at
  several milliseconds a call, and the unit tests stage thousands of
  files. Files are still written, read and replaced as before; only
  the flush to the disk is skipped, and that matters only if the
  machine crashes, which no unit test can observe. A test that needs
  the real calls is marked `real_fsync`, as the durability tests in
  test_stages.py are. The live tests under tests/integration always
  make the real calls.
"""

from __future__ import annotations

import os

# OpenBLAS reads this once, when numpy first loads it, so it is set
# before anything here or in a test module can import numpy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import pytest

_SYNC_CALLS = ("fsync", "fdatasync", "sync")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_fsync: keep the real os.fsync, os.fdatasync and os.sync, "
        "which the unit tests otherwise skip (tests/unit/conftest.py)",
    )


def _skip_sync(*args, **kwargs) -> None:
    """Stands in for the sync calls: flushes nothing."""


@pytest.fixture(autouse=True)
def _no_fsync(request, monkeypatch):
    """Make the sync calls no-ops for the test, unless it is marked
    `real_fsync`. The test's own `monkeypatch` holds the patch, so it
    unwinds in order with any patch the test makes; a test that calls
    `monkeypatch.undo()` makes the real calls from then on."""
    if request.node.get_closest_marker("real_fsync") is not None:
        return
    for name in _SYNC_CALLS:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _skip_sync)


@pytest.fixture(autouse=True)
def _no_claude_config(monkeypatch, tmp_path_factory):
    """Point the claude CLI's config directory at one that does not exist,
    so no unit test reads the developer's own Claude Code login or global
    config (a claude-code client checks the global config when it starts
    and before every call). Tests that need one set their own."""
    missing = tmp_path_factory.getbasetemp() / "no-claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(missing))
