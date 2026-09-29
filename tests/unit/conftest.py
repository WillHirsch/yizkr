"""Fixtures shared by the offline unit tests.

These defaults keep the unit tests fast and self-contained, without
changing what they check:

- **One BLAS thread.** Language detection (py3langid) runs on numpy,
  whose threaded OpenBLAS build starts a worker thread per CPU, and the
  workers spin after every call. `OPENBLAS_NUM_THREADS` is set to 1
  here, before anything imports numpy, unless `OPENBLAS_NUM_THREADS`,
  `OMP_NUM_THREADS` or `MKL_NUM_THREADS` is set and not empty: a thread
  count given in the environment is kept. OpenBLAS reads the variable
  once, as numpy loads, so when numpy is loaded before this file is
  imported (by a plugin, say), the default comes too late, and a
  warning says so.
- **No fsync.** `os.fsync`, `os.fdatasync` and `os.sync` are no-ops in
  every unit test (the `_no_fsync` fixture). The pipeline's atomic
  writes (yizkr.pipeline.stages) fsync every file they stage, at
  several milliseconds a call, and the unit tests stage thousands of
  files. Files are still written, read and replaced as before; only
  the flush to the disk is skipped, and that matters only if the
  machine crashes, which no unit test can observe. The stand-ins for
  `os.fsync` and `os.fdatasync` still check their descriptor with
  `os.fstat`, so a call on one that is not open fails, as the real call
  does. A test that needs the real calls is marked `real_fsync` (the
  marker is registered in pyproject.toml), as the durability tests in
  test_stages.py and test_llmcache.py are. The live tests under
  tests/integration always make the real calls.
- **No pause timer, and the default anomaly thresholds.** A fail-fast
  anomaly check that pauses a slice starts a timer to time the pause
  out (yizkr.pipeline.anomalies), and a unit test that fails a stage
  can pause one. Every unit test records those timers instead of
  starting them (the `_no_anomaly_timers` fixture, which yields the
  RecordedTimer of each), so no thread outlives the test, and forgets
  the checks and the watched slices it made. A test that needs the real
  timer takes the one test_anomalies.py saves. `$YIZKR_ANOMALIES`, an
  operator's file of thresholds, is left out of every unit test, whose
  checks run on the defaults.
- **No record of the real instance.** `$YIZKR_INSTANCE_STATE` names a
  directory of each test's own (the `_no_instance_record` fixture), and
  of the session's own for fixtures that set up before any test's, of a
  module or of the session (`_no_instance_record_in_the_session`). So a
  test that clears or records the instance-contents key
  (yizkr.core.instance), or a child process it starts, never touches the
  developer's record of the one instance, which scratch runs read.
- **No settings from the host.** No unit test reads the host's
  deploy/.env (or ./.env), nor a `YIZKR_*` variable or a connection
  setting (yizkr.core.config) exported in the host's environment (the
  package-scoped `_no_host_settings` fixture). The environment loses
  them, and `resolve_env_file` finds no default env file, as on CI,
  which has none: `env_setting`, `Config.load`, `corpus_dir` and
  `load_exclusions` read only what a test gives them. A test that needs
  a setting gives it explicitly: in the environment, or in an env file
  it names with `$YIZKR_ENV_FILE` or `env_file`. The live tests under
  tests/integration read the real env file.

test_conftest.py tests the first two and the last two, and
test_anomalies.py the third.
"""

from __future__ import annotations

import hashlib
import os
import sys
import warnings
from collections.abc import Container, Iterable, MutableMapping

import pytest

# Each names a thread count that numpy's BLAS library can take from the
# environment: OpenBLAS reads its own variable and OpenMP's, and MKL, which
# some numpy builds use instead, reads its own and OpenMP's.
BLAS_THREAD_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
)


def default_blas_threads(
    environ: MutableMapping[str, str], modules: Container[str]
) -> None:
    """Set `OPENBLAS_NUM_THREADS` to 1 in `environ`, unless one of
    BLAS_THREAD_VARIABLES is set there and not empty. Warn when numpy is
    among the loaded `modules` already, since the default then comes too
    late for this process."""
    if any(environ.get(name) for name in BLAS_THREAD_VARIABLES):
        return
    environ["OPENBLAS_NUM_THREADS"] = "1"
    if "numpy" in modules:
        warnings.warn(
            "numpy was loaded before tests/unit/conftest.py set "
            "OPENBLAS_NUM_THREADS=1, too late for this process, whose "
            "OpenBLAS keeps a thread per CPU; set OPENBLAS_NUM_THREADS=1 "
            "in the environment to run the unit tests with one BLAS thread",
            stacklevel=2,
        )


# OpenBLAS reads the variable once, as numpy loads it, so this runs as the
# file is imported, before any test module can import numpy.
default_blas_threads(os.environ, sys.modules)

# The real os.fstat, kept so that a test that patches os.fstat does not
# change what the stand-ins check.
_fstat = os.fstat


def _skip_fsync(fd) -> None:
    """Stands in for os.fsync and os.fdatasync: flushes nothing, but checks
    the descriptor as the real calls do, so one that is not open still
    fails. Like them, it takes an int or an object with a fileno()
    method."""
    _fstat(fd if isinstance(fd, int) else fd.fileno())


def _skip_sync() -> None:
    """Stands in for os.sync: flushes nothing."""


# each sync call, and what stands in for it
_STAND_INS = {
    "fsync": _skip_fsync,
    "fdatasync": _skip_fsync,
    "sync": _skip_sync,
}


@pytest.fixture(autouse=True)
def _no_fsync(request, monkeypatch):
    """Make the sync calls no-ops for the test, unless it is marked
    `real_fsync`. The test's own `monkeypatch` holds the patch, so it
    unwinds in order with any patch the test makes; a test that calls
    `monkeypatch.undo()` makes the real calls from then on, so a test
    scopes its own patches with `monkeypatch.context()` instead."""
    if request.node.get_closest_marker("real_fsync") is not None:
        return
    for name, stand_in in _STAND_INS.items():
        if hasattr(os, name):
            monkeypatch.setattr(os, name, stand_in)


@pytest.fixture(autouse=True)
def _no_claude_config(monkeypatch, tmp_path_factory):
    """Point the claude CLI's config directory at one that does not exist,
    so no unit test reads the developer's own Claude Code login or global
    config (a claude-code client checks the global config when it starts
    and before every call). Tests that need one set their own."""
    missing = tmp_path_factory.getbasetemp() / "no-claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(missing))


# The stack's connection settings, which Config.load reads from the
# environment before the env file.
CONNECTION_VARIABLES = (
    "WIKIBASE_PUBLIC_HOST",
    "WDQS_PUBLIC_HOST",
    "PUBLIC_SCHEME",
    "HTTP_PORT",
    "MW_ADMIN_NAME",
    "MW_ADMIN_PASS",
    "WIKIBASE_URL",
    "WDQS_SPARQL_URL",
)
# Set by the fixtures here to places of the tests' own, never the host's.
OWN_VARIABLES = frozenset({"YIZKR_INSTANCE_STATE"})


def host_settings(environ: Iterable[str]) -> list[str]:
    """The names in `environ` that no unit test reads from the host: every
    `YIZKR_*` variable but OWN_VARIABLES, and CONNECTION_VARIABLES."""
    return sorted(
        name
        for name in environ
        if (name.startswith("YIZKR_") and name not in OWN_VARIABLES)
        or name in CONNECTION_VARIABLES
    )


@pytest.fixture(scope="package", autouse=True)
def _no_host_settings():
    """Clear the host's settings from the environment, and leave
    resolve_env_file no default env file to find, for every unit test
    and every fixture of theirs. Package scope makes it set up before any
    fixture of a module, which then sets what it needs on top, and undo
    after the last unit test, so the live tests in the same session read
    the real env file."""
    from yizkr.core import config

    with pytest.MonkeyPatch.context() as patch:
        for name in host_settings(os.environ):
            patch.delenv(name)
        patch.setattr(config, "ENV_FILE_CANDIDATES", ())
        yield


@pytest.fixture(scope="session", autouse=True)
def _no_instance_record_in_the_session(tmp_path_factory):
    """The session's own record of the instance ($YIZKR_INSTANCE_STATE),
    for what runs before any test's own (_no_instance_record): a fixture
    of a module or of the session, such as the child sessions that
    test_live_lock.py starts once for its module, whose integration
    conftest clears the instance-contents key."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(
            "YIZKR_INSTANCE_STATE",
            str(
                tmp_path_factory.getbasetemp() / "instance-records" / "session"
            ),
        )
        yield


@pytest.fixture(autouse=True)
def _no_instance_record(request, monkeypatch, tmp_path_factory):
    """Point the record of the instance ($YIZKR_INSTANCE_STATE,
    yizkr.core.instance) at a directory of the test's own, which nothing
    makes until the test writes a record, so no unit test reads or clears
    the developer's record of the instance. Tests that need one set
    their own."""
    own = hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:16]
    monkeypatch.setenv(
        "YIZKR_INSTANCE_STATE",
        str(tmp_path_factory.getbasetemp() / "instance-records" / own),
    )


class RecordedTimer:
    """A timer recorded, never started: when it would call `callback`
    (`seconds`), and whether it was cancelled."""

    def __init__(self, seconds: float, callback) -> None:
        self.seconds = seconds
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture(autouse=True)
def _no_anomaly_timers(monkeypatch):
    """Record the anomaly checks' pause timers instead of starting them,
    and run the checks on their default thresholds; yield the recorded
    timers. The checks and the watched slices the test made are
    forgotten after it, and a timer it started itself is cancelled."""
    from yizkr.pipeline import anomalies

    started: list[RecordedTimer] = []

    def record(seconds, callback):
        started.append(RecordedTimer(seconds, callback))
        return started[-1]

    monkeypatch.setattr(anomalies, "_start_timer", record)
    monkeypatch.delenv(anomalies.CONFIG_VARIABLE, raising=False)
    yield started
    with anomalies._WATCHES_LOCK:
        for watch in anomalies._WATCHES.values():
            for watched in watch._slices.values():
                anomalies._cancel(watched.pending)
        anomalies._WATCHES.clear()
    with anomalies._REGISTRY_LOCK:
        anomalies._REGISTRY.clear()
