"""Fixtures shared by the offline unit tests.

**One BLAS thread.** Language detection (py3langid) runs on numpy, whose
threaded OpenBLAS build starts a worker thread per CPU, and the workers
spin after every call. `OPENBLAS_NUM_THREADS` defaults to 1 here, before
anything imports numpy; a value already set in the environment is kept.
"""

from __future__ import annotations

import os

# OpenBLAS reads this once, when numpy first loads it, so it is set
# before anything here or in a test module can import numpy.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import pytest


@pytest.fixture(autouse=True)
def _no_claude_config(monkeypatch, tmp_path_factory):
    """Point the claude CLI's config directory at one that does not exist,
    so no unit test reads the developer's own Claude Code login or global
    config (a claude-code client checks the global config when it starts
    and before every call). Tests that need one set their own."""
    missing = tmp_path_factory.getbasetemp() / "no-claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(missing))
